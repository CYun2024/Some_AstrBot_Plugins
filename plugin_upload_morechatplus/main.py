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

        # 初始化管理器
        self.context_manager = ContextManager(self.db, self.config)
        self.user_profile_manager = UserProfileManager(
            self.db, self.config, context
        )
        self.model_a_processor = ModelAProcessor(
            self.db, self.context_manager, self.config, context
        )

        # 初始化工具
        self.chat_tools = ChatTools(
            self.db, self.context_manager, self.user_profile_manager
        )

        # 图片缓存
        self._image_cache: dict = {}

        logger.info("[MoreChatPlus] 插件初始化完成")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
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
        """处理消息"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        # 提取消息信息
        msg_info = await self._extract_message_info(event)
        if not msg_info:
            return

        message_id, user_id, nickname, content, has_image, image_urls, reply_to = msg_info
        origin = event.unified_msg_origin

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
        )

        # 检查是否需要触发回复
        should_reply = should_trigger_reply(
            content,
            self.config.core.bot_name,
            self.config.core.bot_qq_id,
            self.config.core.trigger_words,
        )

        # 检查是否需要识图
        need_vision = has_image and should_reply

        if need_vision:
            # 先进行图片识别
            vision_result = await self._process_vision(image_urls, event)
            if vision_result:
                content += f"\n[图片内容: {vision_result}]"

        if should_reply:
            # 触发回复
            await self._trigger_reply(event, message_id, content, user_id, nickname)
        else:
            # 检查是否需要触发总结
            if self.context_manager.should_trigger_summary(origin):
                await self._trigger_summary(origin)

    async def _extract_message_info(
        self,
        event: AstrMessageEvent,
    ) -> Optional[tuple]:
        """提取消息信息"""
        try:
            message_id = str(event.message_obj.message_id or "")
            user_id = str(event.get_sender_id() or "")
            nickname = event.message_obj.sender.nickname or "未知"

            # 提取内容和组件
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
        """处理图片识别"""
        if not image_urls:
            return ""

        try:
            provider_id = self.config.models.vision_provider
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider(event.unified_msg_origin)

            if not provider or not isinstance(provider, Provider):
                logger.warning("[MoreChatPlus] 识图模型不可用")
                return ""

            # 下载第一张图片
            image_url = image_urls[0]
            local_path = await self._resolve_image_to_local(image_url)

            if not local_path:
                return ""

            logger.info(f"[MoreChatPlus] 开始识图: {local_path}")

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=self.config.models.vision_prompt,
                    session_id=uuid.uuid4().hex,
                    image_urls=[local_path],
                    persist=False,
                ),
                timeout=self.config.timeouts.vision_sec,
            )

            result = response.completion_text or ""
            logger.info(f"[MoreChatPlus] 识图结果: {result[:100]}...")
            return result

        except asyncio.TimeoutError:
            logger.error("[MoreChatPlus] 识图超时")
            return ""
        except Exception as e:
            logger.error(f"[MoreChatPlus] 识图失败: {e}")
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

    async def _trigger_reply(
        self,
        event: AstrMessageEvent,
        message_id: str,
        content: str,
        user_id: str,
        nickname: str,
    ):
        """触发回复"""
        origin = event.unified_msg_origin

        logger.info(f"[MoreChatPlus] 触发回复 | origin={origin} user={user_id}")

        # 获取上下文
        context_text = self.context_manager.get_formatted_context(origin)

        # 获取用户画像
        user_profile = self.user_profile_manager.get_profile_summary(user_id, origin)

        # 构建系统提示词
        system_prompt = self.context_manager.build_system_prompt(origin)

        # 格式化新消息
        new_message = format_context_message(
            nickname=nickname,
            user_id=user_id,
            timestamp=time.time(),
            message_id=message_id,
            content=content,
            is_admin=user_id == self.config.core.admin_user_id,
        )

        # 管理员特殊标注
        admin_hint = ""
        if user_id == self.config.core.admin_user_id:
            admin_hint = "\n\n【重要】这条消息来自管理员，请特别注意。"

        # 用户画像提示
        profile_hint = ""
        if user_profile:
            profile_hint = f"\n\n发送者画像: {user_profile}"

        # 构建完整提示词
        full_prompt = f"""{system_prompt}

=== 历史上下文 ===
{context_text}

=== 新消息 ===
{new_message}{admin_hint}{profile_hint}

请回复这条消息。在回复开头使用 [at:{user_id}] 来@发送者。
如果需要引用，使用 <引用:{message_id}>。
"""

        # 调用主LLM
        try:
            provider_id = self.config.models.main_llm_provider
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider(origin)

            if not provider:
                logger.error("[MoreChatPlus] 主LLM不可用")
                return

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=full_prompt,
                    session_id=event.session_id,
                    persist=True,
                ),
                timeout=self.config.timeouts.main_llm_sec,
            )

            reply_text = response.completion_text or ""

            # 清理和构建消息链
            cleaned_text, quote_id, at_ids = clean_message_for_sending(reply_text)

            # 确保有at
            if not at_ids:
                at_ids = [user_id]

            # 构建消息链
            chain = build_message_chain(cleaned_text, quote_id or message_id, at_ids)

            # 最终清理
            chain = final_cleanup_chain(chain)

            if chain:
                # 发送消息
                await event.send(chain)

                # 记录bot回复到上下文
                bot_content = cleaned_text
                bot_msg_id = f"bot_{int(time.time())}"
                self.context_manager.add_message(
                    origin=origin,
                    message_id=bot_msg_id,
                    user_id="bot",
                    nickname=self.config.core.bot_name,
                    content=bot_content,
                )

        except asyncio.TimeoutError:
            logger.error("[MoreChatPlus] 主LLM调用超时")
        except Exception as e:
            logger.error(f"[MoreChatPlus] 回复失败: {e}")

    async def _trigger_summary(self, origin: str):
        """触发上下文总结"""
        if not self.config.active_reply.enable:
            self.context_manager.reset_counter(origin)
            return

        logger.info(f"[MoreChatPlus] 触发上下文总结 | origin={origin}")

        # 调用模型A
        result = await self.model_a_processor.process_context(origin)

        if result:
            self.context_manager.reset_counter(origin)

            # 如果需要主动回复
            if result.should_reply and result.reply_target_msg_id:
                await self._trigger_active_reply(origin, result)

    async def _trigger_active_reply(self, origin: str, summary_result):
        """触发主动回复（已修复：现在会实际发送消息）"""
        logger.info(
            f"[MoreChatPlus] 触发主动回复 | origin={origin} "
            f"target={summary_result.reply_target_msg_id}"
        )

        # 获取目标消息信息
        target_info = self.context_manager.get_message_by_id(
            origin, summary_result.reply_target_msg_id
        )

        if not target_info:
            logger.warning("[MoreChatPlus] 找不到目标消息")
            return

        # 构建主动回复提示词
        context_text = self.context_manager.get_formatted_context(origin)

        # 获取最近的总结
        summaries = self.db.get_recent_summaries(origin, limit=3)
        summary_text = ""
        if summaries:
            summary_parts = [s.summary for s in summaries]
            summary_text = "近期话题:\n" + "\n".join(summary_parts)

        system_prompt = f"""你现在处于一个QQ群聊中。

{summary_text}

模型A的分析和建议:
{summary_result.topic_analysis}

回复建议: {summary_result.reply_suggestion}

请根据以上分析和建议，自然地参与对话。在回复开头使用 [at:目标用户ID] 来@你想回复的人。
"""

        # 格式化目标消息
        target_message = format_context_message(
            nickname=target_info.nickname,
            user_id=target_info.user_id,
            timestamp=target_info.timestamp,
            message_id=target_info.message_id,
            content=target_info.content,
            is_admin=target_info.is_admin,
            reply_to=target_info.reply_to,
        )

        full_prompt = f"""{system_prompt}

=== 历史上下文 ===
{context_text}

=== 要回复的消息 ===
{target_message}

请回复这条消息。
"""

        try:
            provider_id = self.config.models.main_llm_provider
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider(origin)

            if not provider:
                return

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=full_prompt,
                    session_id=uuid.uuid4().hex,
                    persist=False,
                ),
                timeout=self.config.timeouts.main_llm_sec,
            )

            reply_text = response.completion_text or ""

            # 清理和构建消息链
            cleaned_text, quote_id, at_ids = clean_message_for_sending(reply_text)

            # 确保有at
            if not at_ids:
                at_ids = [target_info.user_id]

            chain = build_message_chain(
                cleaned_text,
                quote_id or target_info.message_id,
                at_ids
            )
            chain = final_cleanup_chain(chain)

            if chain:
                # 修复：实际发送消息
                await self._send_active_reply(origin, chain, cleaned_text)

        except Exception as e:
            logger.error(f"[MoreChatPlus] 主动回复失败: {e}")

    async def _send_active_reply(self, origin: str, chain, cleaned_text: str):
        """实际发送主动回复消息"""
        try:
            # 解析平台名称和群号
            # origin 格式通常为: "aiocqhttp:GroupMessage:{group_id}" 或类似
            platform_name = origin.split(':')[0] if ':' in origin else "aiocqhttp"
            
            # 获取平台适配器
            platform = None
            for p in self.context.platforms:
                if p.meta().name == platform_name or platform_name in str(p):
                    platform = p
                    break
            
            if not platform:
                logger.error(f"[MoreChatPlus] 找不到平台适配器: {platform_name}")
                return
            
            # 发送消息
            await platform.send_message(unified_msg_origin=origin, message_chain=chain)
            logger.info(f"[MoreChatPlus] 主动回复已发送: {cleaned_text[:100]}...")

            # 记录到上下文
            bot_msg_id = f"bot_active_{int(time.time())}"
            self.context_manager.add_message(
                origin=origin,
                message_id=bot_msg_id,
                user_id="bot",
                nickname=self.config.core.bot_name,
                content=cleaned_text,
            )

        except Exception as e:
            logger.error(f"[MoreChatPlus] 发送主动回复失败: {e}")

    # ==================== LLM工具 ====================

    @llm_tool(name="morechatplus_get_message")
    async def tool_get_message(
        self,
        event: AstrMessageEvent,
        message_id: str,
    ):
        """获取指定消息的完整内容"""
        return await self.chat_tools.get_message_content(event, message_id)

    @llm_tool(name="morechatplus_get_user_profile")
    async def tool_get_user_profile(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ):
        """获取用户画像"""
        return await self.chat_tools.get_user_profile(event, user_id)

    @llm_tool(name="morechatplus_query_nickname")
    async def tool_query_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str,
    ):
        """查询昵称"""
        return await self.chat_tools.query_nickname(event, nickname)

    @llm_tool(name="morechatplus_get_context")
    async def tool_get_context(
        self,
        event: AstrMessageEvent,
        count: int = 20,
    ):
        """获取最近上下文"""
        return await self.chat_tools.get_recent_context(event, count)

    @llm_tool(name="morechatplus_add_nickname")
    async def tool_add_nickname(
        self,
        event: AstrMessageEvent,
        user_id: str,
        nickname: str,
    ):
        """添加用户昵称"""
        return await self.chat_tools.add_user_nickname(event, user_id, nickname)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[MoreChatPlus] 插件终止")
