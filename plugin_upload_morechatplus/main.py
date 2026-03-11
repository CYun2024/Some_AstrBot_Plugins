"""MoreChatPlus

QQ群聊增强插件，提供：
- 上下文管理和总结
- 用户画像维护
- 主动回复判定
- 图片识别和缓存（插件自主管理存储）
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
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import Provider, ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import download_image_by_url

from .context_manager import ContextManager
from .database import DatabaseManager
from .image_cache import ImageCacheManager
from .message_utils import (
    build_message_chain,
    clean_message_for_sending,
    convert_at_tags_to_components,
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

        # 初始化数据库和图片存储
        plugin_data_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "morechatplus"
        )
        self.db = DatabaseManager(plugin_data_dir / "chat_data.db")

        # 初始化图片缓存（插件自主管理存储目录）
        images_dir = plugin_data_dir / "images"
        self.image_cache = ImageCacheManager(
            db_path=plugin_data_dir / "image_cache.db",
            images_dir=images_dir,
            max_cache_size=self.config.image_cache.max_cache_size
        )

        # 不再缓存debugger实例，改为动态获取以确保可靠
        self._debugger = None
        self._debugger_last_try = 0
        self._debugger_retry_count = 0  # 新增：重试计数

        # 初始化管理器
        self.context_manager = ContextManager(self.db, self.config)
        self.user_profile_manager = UserProfileManager(
            self.db, self.config, context, debugger=self
        )
        self.model_a_processor = ModelAProcessor(
            self.db, self.context_manager, self.config, context, debugger=self
        )

        # 初始化工具
        self.chat_tools = ChatTools(
            self.db, self.context_manager, self.user_profile_manager, self.image_cache
        )

        # 缓存
        self._pending_active_replies: dict = {}

        logger.info(f"[MoreChatPlus] 插件初始化完成，图片缓存目录: {images_dir}")

        # 关键：延迟获取 debugger，确保 LLMDebugger 已注册
        asyncio.create_task(self._delayed_init_debugger())

    async def _delayed_init_debugger(self):
        """延迟初始化 debugger 连接"""
        await asyncio.sleep(3)  # 等待3秒，确保 LLMDebugger 已加载
        debugger = self._get_llm_debugger()
        if debugger:
            logger.info("[MoreChatPlus] 延迟初始化成功连接到 LLM Debugger")
        else:
            logger.warning("[MoreChatPlus] 延迟初始化未能连接到 LLM Debugger，将在上报时重试")

    def _get_llm_debugger(self):
        """动态获取 LLM Debugger 实例（带缓存刷新机制）- 增强版"""
        now = time.time()

        # 如果已有实例，直接返回
        if self._debugger is not None:
            return self._debugger

        # 限制重试频率，但允许前5次快速重试
        retry_delay = 5 if self._debugger_retry_count < 5 else 30
        if now - self._debugger_last_try < retry_delay:
            return None

        self._debugger_last_try = now
        self._debugger_retry_count += 1

        try:
            # 方法1: 从 _plugin_instances 获取
            if hasattr(self.context, '_plugin_instances'):
                self._debugger = self.context._plugin_instances.get('llm_debugger')
                if self._debugger:
                    logger.info(f"[MoreChatPlus] 成功连接到 LLM Debugger (via _plugin_instances, 尝试#{self._debugger_retry_count})")
                    self._debugger_retry_count = 0  # 重置计数
                    return self._debugger

            # 方法2: 从 star_registry 获取
            if not self._debugger and hasattr(self.context, 'star_registry'):
                registry = self.context.star_registry
                if isinstance(registry, dict):
                    self._debugger = registry.get('llm_debugger')
                elif hasattr(registry, 'get'):
                    self._debugger = registry.get('llm_debugger')

                if self._debugger:
                    logger.info(f"[MoreChatPlus] 成功连接到 LLM Debugger (via star_registry, 尝试#{self._debugger_retry_count})")
                    self._debugger_retry_count = 0
                    return self._debugger

            # 方法3: 遍历所有插件查找
            if not self._debugger and hasattr(self.context, '_plugin_instances'):
                for name, instance in self.context._plugin_instances.items():
                    if 'debugger' in name.lower() or hasattr(instance, 'record_llm_call'):
                        self._debugger = instance
                        logger.info(f"[MoreChatPlus] 找到可能的 Debugger: {name}")
                        self._debugger_retry_count = 0
                        return self._debugger

            logger.debug(f"[MoreChatPlus] 获取Debugger失败 (尝试#{self._debugger_retry_count})")

        except Exception as e:
            logger.debug(f"[MoreChatPlus] 获取Debugger失败: {e}")

        return self._debugger

    async def safe_record_llm_call(self, data: dict):
        """安全地上报LLM调用，带错误处理和重试 - 增强版"""
        debugger = self._get_llm_debugger()

        if not debugger:
            # 如果 debugger 不存在且重试次数不多，安排延迟重试上报
            if self._debugger_retry_count < 10:
                logger.debug(f"[MoreChatPlus] Debugger 不可用，将延迟上报 (重试#{self._debugger_retry_count})")
                asyncio.create_task(self._delayed_record_llm_call(data, delay=5))
            return

        try:
            # 确保必需字段存在
            if "timestamp" not in data:
                data["timestamp"] = time.time()
            if "source" not in data:
                data["source"] = {"plugin": "morechatplus", "purpose": "unknown"}
            if "conversation_id" not in data:
                data["conversation_id"] = uuid.uuid4().hex

            # 确保 sender 字段存在（如果外部未提供）
            if "sender" not in data:
                data["sender"] = {"id": "unknown", "name": "unknown"}

            if hasattr(debugger, 'record_llm_call'):
                await debugger.record_llm_call(data)
                logger.debug(f"[MoreChatPlus] 成功上报LLM调用: phase={data.get('phase')}, purpose={data.get('source', {}).get('purpose')}")
            else:
                logger.warning("[MoreChatPlus] Debugger 没有 record_llm_call 方法")

        except Exception as e:
            logger.error(f"[MoreChatPlus] 上报LLM调用失败: {e}")

    async def _delayed_record_llm_call(self, data: dict, delay: float):
        """延迟上报 LLM 调用"""
        await asyncio.sleep(delay)
        # 再次尝试获取 debugger 并上报
        debugger = self._get_llm_debugger()
        if debugger:
            try:
                if hasattr(debugger, 'record_llm_call'):
                    await debugger.record_llm_call(data)
                    logger.info(f"[MoreChatPlus] 延迟上报成功: phase={data.get('phase')}")
            except Exception as e:
                logger.error(f"[MoreChatPlus] 延迟上报失败: {e}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
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

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前处理 [at:QQ号] 标签"""
        if not self.config.core.enable:
            return

        try:
            # 获取消息链
            message_chain = event.get_messages()
            if not message_chain:
                return

            # 处理消息链中的 [at:QQ号] 标签
            new_chain = []
            for comp in message_chain:
                if isinstance(comp, Plain):
                    # 转换 [at:QQ号] 标签为 At 组件
                    converted = convert_at_tags_to_components(comp.text)
                    new_chain.extend(converted)
                else:
                    new_chain.append(comp)

            # 更新消息链
            if new_chain:
                event.message_obj.message = new_chain
                logger.debug(f"[MoreChatPlus] 已处理消息链中的at标签")

        except Exception as e:
            logger.debug(f"[MoreChatPlus] 处理at标签失败: {e}")

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

        message_id, user_id, nickname, content, has_image, image_urls, reply_to, image_ids = msg_info
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
            vision_result = await self._process_vision(image_urls, image_ids, event)
            if vision_result:
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
            event._morechatplus_processed = True
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

        # 关键修改：不再直接替换消息链，而是将增强后的prompt存储在事件上
        # 供 on_llm_request 使用，确保可靠注入
        event._morechatplus_enhanced_prompt = enhanced_prompt
        event._morechatplus_processed = True

        logger.info("[MoreChatPlus] 消息已增强，等待LLM请求阶段注入上下文")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在LLM请求前注入上下文 - 修复版"""
        if not self.config.core.enable:
            return
        
        # 检查是否有预处理的增强prompt
        if not hasattr(event, '_morechatplus_enhanced_prompt'):
            return
            
        enhanced_prompt = event._morechatplus_enhanced_prompt
        injected = False
        
        try:
            # 方法1: 直接修改 prompt 属性（AstrBot 最常用）
            if hasattr(req, 'prompt'):
                original = req.prompt
                req.prompt = enhanced_prompt
                logger.info(f"[MoreChatPlus] 已注入到 req.prompt | 原长度: {len(str(original))} | 新长度: {len(enhanced_prompt)}")
                injected = True
            
            # 方法2: 修改 messages 列表（OpenAI格式）
            elif hasattr(req, 'messages') and isinstance(req.messages, list):
                for i in range(len(req.messages) - 1, -1, -1):
                    msg = req.messages[i]
                    if isinstance(msg, dict) and msg.get('role') == 'user':
                        msg['content'] = enhanced_prompt
                        logger.info(f"[MoreChatPlus] 已注入到 messages[{i}].content")
                        injected = True
                        break
            
            # 方法3: 修改 contexts（历史上下文，关键修复！）
            elif hasattr(req, 'contexts') and isinstance(req.contexts, list):
                for i in range(len(req.contexts) - 1, -1, -1):
                    ctx = req.contexts[i]
                    if isinstance(ctx, dict) and ctx.get('role') == 'user':
                        ctx['content'] = enhanced_prompt
                        logger.info(f"[MoreChatPlus] 已注入到 contexts[{i}].content")
                        injected = True
                        break
            
            if injected:
                # 同步更新 event 的 message_str
                if hasattr(event, 'message_str'):
                    event.message_str = enhanced_prompt
                delattr(event, '_morechatplus_enhanced_prompt')
                logger.info("[MoreChatPlus] 上下文注入成功")
            else:
                logger.error(f"[MoreChatPlus] 注入失败，req属性: {[a for a in dir(req) if not a.startswith('_')]}")
                
        except Exception as e:
            logger.error(f"[MoreChatPlus] 注入异常: {e}")

    async def _extract_message_info(
        self,
        event: AstrMessageEvent,
    ) -> Optional[tuple]:
        """提取消息信息（插件自主管理图片存储，MD5去重）"""
        try:
            message_id = str(event.message_obj.message_id or "")
            user_id = str(event.get_sender_id() or "")
            nickname = event.message_obj.sender.nickname or "未知"

            content_parts = []
            has_image = False
            image_urls = []
            image_ids = []
            reply_to = ""

            for comp in event.get_messages():
                if isinstance(comp, Plain):
                    content_parts.append(comp.text)
                elif isinstance(comp, Image):
                    has_image = True
                    url = str(comp.url or comp.file or "").strip()
                    if url:
                        image_urls.append(url)
                        # 下载/获取图片到临时路径
                        temp_path = await self._resolve_image_to_local(url)

                        if temp_path and Path(temp_path).exists():
                            # 保存到插件缓存（自动MD5去重）
                            # save_image 会：
                            # 1. 计算MD5检查是否已存在
                            # 2. 如果存在：删除temp_path，返回已有ID和路径
                            # 3. 如果不存在：复制到插件目录，删除temp_path，返回新ID和路径
                            img_id, exists, final_path = self.image_cache.save_image(url, temp_path)
                            image_ids.append(img_id)

                            status = "命中" if exists else "新图片"
                            logger.debug(f"[MoreChatPlus] 图片处理[{status}]: {img_id}")
                            content_parts.append(f"[image:{len(image_urls)}:{img_id}]")
                        else:
                            content_parts.append(f"[image:{len(image_urls)}:unknown]")
                    else:
                        content_parts.append(f"[image:{len(image_urls)}:unknown]")
                elif isinstance(comp, Reply):
                    reply_to = str(comp.id or "")
                    content_parts.append(f"<引用:{reply_to}>")
                elif isinstance(comp, At):
                    content_parts.append(f"[at:{comp.qq}]")

            content = " ".join(content_parts)
            return message_id, user_id, nickname, content, has_image, image_urls, reply_to, image_ids

        except Exception as e:
            logger.error(f"[MoreChatPlus] 提取消息信息失败: {e}")
            return None

    async def _process_vision(
        self,
        image_urls: List[str],
        image_ids: List[str],
        event: AstrMessageEvent,
    ) -> str:
        """处理图片识别，带缓存和完整上报"""
        if not image_urls or not image_ids:
            return ""

        # 优先使用图片ID获取缓存的识图结果
        for img_id in image_ids:
            cached_result = self.image_cache.get_vision_result(img_id)
            if cached_result:
                logger.info(f"[MoreChatPlus] 使用缓存的识图结果: {img_id}")
                # 增加发送计数（识图结果被使用）
                self.image_cache.increment_send_count(img_id)
                return cached_result

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

            # 使用第一个图片ID获取本地路径
            image_id = image_ids[0]
            local_path = self.image_cache.get_local_path(image_id)

            if not local_path or not Path(local_path).exists():
                logger.warning(f"[MoreChatPlus] 图片文件不存在: {image_id}")
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

            # 保存识图结果到缓存
            self.image_cache.set_vision_result(image_id, result)
            # 增加发送计数（识图结果被使用）
            self.image_cache.increment_send_count(image_id)

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
        """解析图片到本地路径（使用AstrBot的下载功能）"""
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
        """构建增强后的prompt（区分主动/被动回复）"""
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

        # 关键修改：只有主动回复（非@触发）时才附加建议
        active_reply_hint = ""
        if active_reply_info:
            # 检查当前是否是被@触发的（通过检查original_content中是否有[at:bot_qq_id]）
            is_mentioned = self.config.core.bot_qq_id and f"[at:{self.config.core.bot_qq_id}]" in original_content
            if not is_mentioned:
                # 只有主动回复（非@触发）时才附加建议
                active_reply_hint = (
                    f"\n\n【主动回复建议】\n"
                    f"场景分析: {active_reply_info.topic_analysis}\n"
                    f"回复策略: {active_reply_info.reply_suggestion}"
                )
            else:
                # 被@时不附加建议，让主LLM自行处理
                logger.debug("[MoreChatPlus] 被@触发，不附加模型A建议")

        enhanced_prompt = f"""{system_prompt}

=== 历史上下文 ===
{context_text}

=== 当前消息 ===
{new_message}{admin_hint}{profile_hint}{vision_hint}{active_reply_hint}

请回复这条消息。在回复开头使用 [at:{user_id}] 来@发送者。
如果需要引用，使用 <引用:{message_id}>。
"""

        return enhanced_prompt

    # ==================== LLM 工具函数 ====================

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

    @llm_tool(name="morechatplus_get_image_vision")
    async def tool_get_image_vision(self, event: AstrMessageEvent, image_id: str):
        """获取图片的识图结果"""
        return await self.chat_tools.get_image_vision_result(event, image_id)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[MoreChatPlus] 插件终止")