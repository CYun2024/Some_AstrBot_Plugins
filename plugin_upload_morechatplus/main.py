"""MoreChatPlus

QQ群聊增强插件，提供：
- 上下文管理和总结
- 用户画像维护
- 主动回复判定
- 图片识别
- 艾特功能
"""

import asyncio
import base64
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

from astrbot.api import logger, star, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import Provider, ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import download_image_by_url

from .context_manager import ContextManager
from .database import DatabaseManager
from .message_utils import (
    build_message_chain,
    clean_message_for_sending,
    final_cleanup_chain,
    format_context_message,
    parse_at_tags,
    should_trigger_reply,
)
from .model_a_processor import ModelAProcessor
from .plugin_config import PluginConfig, parse_plugin_config
from .tools import ChatTools
from .user_profile_manager import UserProfileManager


class MoreChatPlusPlugin(star.Star):
    """增强聊天插件"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = parse_plugin_config(config)

        # 初始化数据库
        plugin_data_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "morechatplus"
        )
        self.db = DatabaseManager(plugin_data_dir / "chat_data.db")

        # 不再缓存debugger实例，改为动态获取以确保可靠
        self._debugger = None
        self._debugger_last_try = 0

        # 初始化管理器
        self.context_manager = ContextManager(self.db, self.config)
        self.user_profile_manager = UserProfileManager(
            self.db, self.config, context, debugger=self  # 传入self，内部会调用我们的safe_record
        )
        self.model_a_processor = ModelAProcessor(
            self.db, self.context_manager, self.config, context, debugger=self
        )

        # 初始化工具
        self.chat_tools = ChatTools(
            self.db, self.context_manager, self.user_profile_manager
        )

        # 缓存
        self._image_cache: dict = {}
        self._vision_cache: dict = {}
        self._pending_active_replies: dict = {}

        logger.info("[MoreChatPlus] 插件初始化完成")

    def _get_llm_debugger(self):
        """动态获取 LLM Debugger 实例（带缓存刷新机制）"""
        now = time.time()
        # 每5秒才重新尝试获取，避免频繁调用
        if self._debugger is None and now - self._debugger_last_try > 5:
            self._debugger_last_try = now
            try:
                # 尝试从context获取
                if hasattr(self.context, '_plugin_instances'):
                    self._debugger = self.context._plugin_instances.get('llm_debugger')
                if not self._debugger and hasattr(self.context, 'star_registry'):
                    if isinstance(self.context.star_registry, dict):
                        self._debugger = self.context.star_registry.get('llm_debugger')
                if self._debugger:
                    logger.info("[MoreChatPlus] 成功连接到 LLM Debugger")
            except Exception as e:
                logger.debug(f"[MoreChatPlus] 获取Debugger失败: {e}")
        return self._debugger

    async def safe_record_llm_call(self, data: dict):
        """安全地上报LLM调用，带错误处理和重试"""
        debugger = self._get_llm_debugger()
        if not debugger:
            return

        try:
            # 确保必要字段存在
            if "timestamp" not in data:
                data["timestamp"] = time.time()
            if "source" not in data:
                data["source"] = {"plugin": "morechatplus", "purpose": "unknown"}

            # 调用debugger的记录方法
            if hasattr(debugger, 'record_llm_call'):
                await debugger.record_llm_call(data)
            else:
                logger.debug("[MoreChatPlus] Debugger没有record_llm_call方法")
        except Exception as e:
            # 上报失败不影响主业务
            logger.debug(f"[MoreChatPlus] 上报LLM调用失败: {e}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
        # 延迟获取debugger，确保其他插件已加载
        await asyncio.sleep(1)
        self._get_llm_debugger()

        if self.config.core.enable:
            await self.user_profile_manager.initialize()
            logger.info(
                f"[MoreChatPlus] 插件已启用 | "
                f"bot_name={self.config.core.bot_name} | "
                f"trigger_words={self.config.core.trigger_words}"
            )
        else:
            logger.info("[MoreChatPlus] 插件已禁用")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理消息 - 增强上下文，但不直接回复"""
        logger.info(f"[MoreChatPlus] 收到消息: {event.message_str}")

        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        # 防止循环处理
        if hasattr(event, '_morechatplus_processed') and event._morechatplus_processed:
            logger.debug("[MoreChatPlus] 消息已处理，跳过")
            return

        # 提取消息信息
        msg_info = await self._extract_message_info(event)
        if not msg_info:
            logger.warning("[MoreChatPlus] 提取消息信息失败")
            return

        message_id, user_id, nickname, content, has_image, image_urls, reply_to = msg_info
        origin = event.unified_msg_origin

        logger.info(f"[MoreChatPlus] 来源={origin}, 用户={nickname}({user_id}), 内容={content[:50]}...")

        # 检查是否是管理员
        is_admin = user_id == self.config.core.admin_user_id

        # 添加到上下文
        self.context_manager.add_message(
            origin=origin,
            message_id=message_id,
            user_id=user_id,
            nickname=nickname,
            content=content,
            has_image=has_image,
            image_urls=image_urls,
            is_admin=is_admin,
            reply_to=reply_to,
            count_towards_summary=True,
        )

        # 检查是否需要触发回复
        should_reply = should_trigger_reply(
            content,
            self.config.core.bot_name,
            self.config.core.bot_qq_id,
            self.config.core.trigger_words,
        )
        logger.info(f"[MoreChatPlus] should_reply={should_reply}")

        # 检查是否需要识图
        vision_result = None
        if should_reply and has_image:
            vision_result = await self._process_vision(image_urls, event)
            if vision_result:
                self._vision_cache[message_id] = vision_result
                logger.info(f"[MoreChatPlus] 识图完成，结果长度={len(vision_result)}")

        # 触发上下文总结
        if self.context_manager.should_trigger_summary(origin):
            logger.info(f"[MoreChatPlus] 触发异步总结 for {origin}")
            asyncio.create_task(self._trigger_summary(origin))

        # 检查待处理的主动回复
        active_reply_info = self._check_pending_active_reply(origin)
        if active_reply_info:
            logger.info(f"[MoreChatPlus] 发现待处理主动回复")

        need_final_reply = should_reply or active_reply_info is not None

        if not need_final_reply:
            logger.info("[MoreChatPlus] 无需回复，退出")
            return

        # 构建增强后的prompt
        enhanced_prompt = await self._build_enhanced_prompt(
            event=event,
            original_content=content,
            user_id=user_id,
            nickname=nickname,
            message_id=message_id,
            vision_result=vision_result,
            active_reply_info=active_reply_info,
        )

        # 确保包含Bot名字
        if self.config.core.bot_name and self.config.core.bot_name not in enhanced_prompt[:50]:
            enhanced_prompt = f"{self.config.core.bot_name}，请回复：\n\n{enhanced_prompt}"

        logger.info(f"[MoreChatPlus] 增强后的prompt预览: {enhanced_prompt[:200]}...")

        # 替换原消息
        new_chain = [Plain(enhanced_prompt)]
        event.message_obj.message = new_chain
        event._morechatplus_processed = True

        logger.info("[MoreChatPlus] 消息已增强，继续传播")

    async def _extract_message_info(
        self,
        event: AstrMessageEvent,
    ) -> Optional[tuple]:
        """提取消息信息"""
        try:
            message_id = str(event.message_obj.message_id or "")
            user_id = str(event.get_sender_id() or "")
            nickname = event.message_obj.sender.nickname or "未知"

            content_parts = []
            has_image = False
            image_urls = []
            reply_to = ""

            for comp in event.get_messages():
                if isinstance(comp, Plain):
                    content_parts.append(comp.text)
                elif isinstance(comp, Image):
                    has_image = True
                    url = str(comp.url or comp.file or "").strip()
                    if url:
                        image_urls.append(url)
                    content_parts.append(f"[image:{len(image_urls)}]")
                elif isinstance(comp, Reply):
                    reply_to = str(comp.id or "")
                    reply_text = (comp.message_str or "").strip()[:50]
                    content_parts.append(f"[引用:{reply_to}] {reply_text}")
                elif isinstance(comp, At):
                    content_parts.append(f"[at:{comp.qq}]")

            content = " ".join(content_parts)
            return message_id, user_id, nickname, content, has_image, image_urls, reply_to

        except Exception as e:
            logger.error(f"[MoreChatPlus] 提取消息信息失败: {e}")
            return None

    async def _process_vision(
        self,
        image_urls: List[str],
        event: AstrMessageEvent,
    ) -> str:
        """处理图片识别，带完整上报"""
        if not image_urls:
            return ""

        provider_id = self.config.models.vision_provider
        provider = None
        conv_id = uuid.uuid4().hex

        try:
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider(event.unified_msg_origin)

            if not provider or not isinstance(provider, Provider):
                logger.warning("[MoreChatPlus] 识图模型不可用")
                return ""

            image_url = image_urls[0]
            local_path = await self._resolve_image_to_local(image_url)

            if not local_path:
                return ""

            logger.info(f"[MoreChatPlus] 开始识图: {local_path}")

            # 上报请求
            await self.safe_record_llm_call({
                "phase": "request",
                "provider_id": provider_id or "default",
                "model": getattr(provider, 'model', 'vision'),
                "prompt": self.config.models.vision_prompt,
                "images": [local_path],
                "source": {"plugin": "morechatplus", "purpose": "vision"},
                "sender": {"id": event.get_sender_id(), "name": event.get_sender_name()},
                "conversation_id": conv_id,
                "timestamp": time.time()
            })

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=self.config.models.vision_prompt,
                    session_id=conv_id,
                    image_urls=[local_path],
                    persist=False,
                ),
                timeout=self.config.timeouts.vision_sec,
            )

            result = response.completion_text or ""
            logger.info(f"[MoreChatPlus] 识图结果: {result[:100]}...")

            # 上报响应
            await self.safe_record_llm_call({
                "phase": "response",
                "provider_id": provider_id or "default",
                "model": getattr(provider, 'model', 'vision'),
                "response": result,
                "usage": getattr(response, 'usage', None),
                "source": {"plugin": "morechatplus", "purpose": "vision"},
                "conversation_id": conv_id,
                "timestamp": time.time()
            })

            return result

        except asyncio.TimeoutError:
            logger.error("[MoreChatPlus] 识图超时")
            # 上报错误
            await self.safe_record_llm_call({
                "phase": "response",
                "provider_id": provider_id or "default",
                "model": getattr(provider, 'model', 'vision') if provider else "unknown",
                "response": "[识图超时]",
                "source": {"plugin": "morechatplus", "purpose": "vision_error"},
                "conversation_id": conv_id,
                "timestamp": time.time()
            })
            return ""
        except Exception as e:
            logger.error(f"[MoreChatPlus] 识图失败: {e}")
            # 上报错误
            await self.safe_record_llm_call({
                "phase": "response",
                "provider_id": provider_id or "default",
                "model": getattr(provider, 'model', 'vision') if provider else "unknown",
                "response": f"[识图错误: {str(e)}]",
                "source": {"plugin": "morechatplus", "purpose": "vision_error"},
                "conversation_id": conv_id,
                "timestamp": time.time()
            })
            return ""

    async def _resolve_image_to_local(self, image_ref: str) -> str:
        """解析图片到本地路径"""
        clean_ref = str(image_ref or "").strip()
        if not clean_ref:
            return ""

        if clean_ref.startswith("file://"):
            clean_ref = clean_ref[7:]

        candidate = Path(clean_ref)
        if candidate.exists() and candidate.is_file():
            return str(candidate)

        if clean_ref.startswith("http://") or clean_ref.startswith("https://"):
            try:
                downloaded = await download_image_by_url(clean_ref)
                return str(downloaded or "")
            except Exception as e:
                logger.error(f"[MoreChatPlus] 下载图片失败: {e}")
                return ""

        return ""

    async def _trigger_summary(self, origin: str):
        """触发上下文总结"""
        if not self.config.active_reply.enable:
            self.context_manager.reset_counter(origin)
            return

        self.context_manager.set_summarizing(origin, True)
        try:
            logger.info(f"[MoreChatPlus] 触发上下文总结 | origin={origin}")

            # 调用模型A（内部已上报，但我们也在这里包装一层确保）
            result = await self.model_a_processor.process_context(origin)

            if result:
                self.context_manager.reset_counter(origin)

                if result.should_reply and result.reply_target_msg_id:
                    self._pending_active_replies[origin] = result
                    logger.info(f"[MoreChatPlus] 已保存主动回复待处理")
        finally:
            self.context_manager.set_summarizing(origin, False)

    def _check_pending_active_reply(self, origin: str) -> Optional[dict]:
        """检查待处理的主动回复"""
        return self._pending_active_replies.pop(origin, None)

    async def _build_enhanced_prompt(
        self,
        event: AstrMessageEvent,
        original_content: str,
        user_id: str,
        nickname: str,
        message_id: str,
        vision_result: Optional[str] = None,
        active_reply_info: Optional[dict] = None,
    ) -> str:
        """构建增强后的prompt"""
        origin = event.unified_msg_origin

        context_text = self.context_manager.get_formatted_context(origin)
        user_profile = self.user_profile_manager.get_profile_summary(user_id, origin)
        system_prompt = self.context_manager.build_system_prompt(origin)

        new_message = format_context_message(
            nickname=nickname,
            user_id=user_id,
            timestamp=time.time(),
            message_id=message_id,
            content=original_content,
            is_admin=user_id == self.config.core.admin_user_id,
        )

        admin_hint = ""
        if user_id == self.config.core.admin_user_id:
            admin_hint = "\n\n【重要】这条消息来自管理员，请特别注意。"

        profile_hint = ""
        if user_profile:
            profile_hint = f"\n\n发送者画像: {user_profile}"

        vision_hint = ""
        if vision_result:
            vision_hint = f"\n\n图片识别结果: {vision_result}"

        active_reply_hint = ""
        if active_reply_info:
            active_reply_hint = (
                f"\n\n【主动回复建议】\n"
                f"模型A分析: {active_reply_info.topic_analysis}\n"
                f"回复建议: {active_reply_info.reply_suggestion}\n"
                f"目标消息ID: #{active_reply_info.reply_target_msg_id}"
            )

        enhanced_prompt = f"""{system_prompt}

=== 历史上下文 ===
{context_text}

=== 当前消息 ===
{new_message}{admin_hint}{profile_hint}{vision_hint}{active_reply_hint}

请回复这条消息。在回复开头使用 [at:{user_id}] 来@发送者。
如果需要引用，使用 <引用:{message_id}>。
"""

        return enhanced_prompt

    @llm_tool(name="morechatplus_get_message")
    async def tool_get_message(self, event: AstrMessageEvent, message_id: str):
        """获取指定消息"""
        return await self.chat_tools.get_message_content(event, message_id)

    @llm_tool(name="morechatplus_get_user_profile")
    async def tool_get_user_profile(self, event: AstrMessageEvent, user_id: str):
        """获取用户画像"""
        return await self.chat_tools.get_user_profile(event, user_id)

    @llm_tool(name="morechatplus_query_nickname")
    async def tool_query_nickname(self, event: AstrMessageEvent, nickname: str):
        """查询昵称"""
        return await self.chat_tools.query_nickname(event, nickname)

    @llm_tool(name="morechatplus_get_context")
    async def tool_get_context(self, event: AstrMessageEvent, count: int = 20):
        """获取最近上下文"""
        return await self.chat_tools.get_recent_context(event, count)

    @llm_tool(name="morechatplus_add_nickname")
    async def tool_add_nickname(self, event: AstrMessageEvent, user_id: str, nickname: str):
        """添加用户昵称"""
        return await self.chat_tools.add_user_nickname(event, user_id, nickname)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[MoreChatPlus] 插件终止")