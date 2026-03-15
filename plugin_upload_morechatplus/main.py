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
        self._debugger_retry_count = 0

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
        # 缓存Bot消息：origin -> {msg_id: user_id}
        self._bot_message_cache: dict = {}
        # 新增：缓存待发送的Bot消息内容，避免重复记录
        self._pending_bot_messages: dict = {}  # origin -> (message_id, content)

        logger.info(f"[MoreChatPlus] 插件初始化完成")
        asyncio.create_task(self._delayed_init_debugger())


    def _log_debug(self, msg: str):
        """根据配置输出调试日志"""
        if self.config.core.debug:
            logger.info(f"[MoreChatPlus] {msg}")
        else:
            logger.debug(f"[MoreChatPlus] {msg}")
    async def _delayed_init_debugger(self):
        """延迟初始化 debugger 连接"""
        await asyncio.sleep(3)
        self._get_llm_debugger()

    def _get_llm_debugger(self):
        """动态获取 LLM Debugger 实例"""
        now = time.time()
        if self._debugger is not None:
            return self._debugger
        retry_delay = 5 if self._debugger_retry_count < 5 else 30
        if now - self._debugger_last_try < retry_delay:
            return None
        self._debugger_last_try = now
        self._debugger_retry_count += 1
        try:
            if hasattr(self.context, '_plugin_instances'):
                self._debugger = self.context._plugin_instances.get('llm_debugger')
            if not self._debugger and hasattr(self.context, 'star_registry'):
                registry = self.context.star_registry
                self._debugger = registry.get('llm_debugger') if isinstance(registry, dict) else None
        except Exception as e:
            logger.debug(f"[MoreChatPlus] 获取Debugger失败: {e}")
        return self._debugger

    async def safe_record_llm_call(self, data: dict):
        """安全地上报LLM调用"""
        debugger = self._get_llm_debugger()
        if not debugger:
            return
        try:
            if "timestamp" not in data:
                data["timestamp"] = time.time()
            if "source" not in data:
                data["source"] = {"plugin": "morechatplus", "purpose": "unknown"}
            if "conversation_id" not in data:
                data["conversation_id"] = uuid.uuid4().hex
            if "sender" not in data:
                data["sender"] = {"id": "unknown", "name": "unknown"}
            if hasattr(debugger, 'record_llm_call'):
                await debugger.record_llm_call(data)
        except Exception as e:
            logger.error(f"[MoreChatPlus] 上报LLM调用失败: {e}")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
        await asyncio.sleep(1)
        self._get_llm_debugger()
        if self.config.core.enable:
            await self.user_profile_manager.initialize()
            logger.info(f"[MoreChatPlus] 插件已启用")
        else:
            logger.info("[MoreChatPlus] 插件已禁用")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """捕获LLM响应并记录为Bot消息（主要记录方式）"""
        if not self.config.core.enable:
            return

        try:
            bot_id = str(self.config.core.bot_qq_id or "")
            if not bot_id:
                return

            # 获取回复内容
            reply_text = response.completion_text or "" if hasattr(response, 'completion_text') else str(response)
            if not reply_text or not reply_text.strip():
                return

            origin = event.unified_msg_origin

            # 生成唯一消息ID（后续on_decorating_result如果触发可以关联）
            message_id = f"bot_{uuid.uuid4().hex[:12]}"

            # 尝试提取引用信息（从响应中查找<引用:xxx>格式）
            reply_to = ""
            import re
            ref_match = re.search(r'<引用[:\s]?(\d+)>', reply_text)
            if ref_match:
                reply_to = ref_match.group(1)

            # 清理内容中的标签，保留纯文本用于记录
            clean_content = reply_text
            # 移除引用标签（保留引用关系在reply_to中）
            clean_content = re.sub(r'<引用[^>]*>', '', clean_content)
            # 移除at标签（保留在内容中用于显示，但也可以清理）
            # clean_content = re.sub(r'\[at:\d+\]', '', clean_content)
            clean_content = clean_content.strip()

            # 保存到数据库
            self.context_manager.add_message(
                origin=origin,
                message_id=message_id,
                user_id=bot_id,
                nickname=self.config.core.bot_name,
                content=clean_content,
                has_image=False,  # LLM文本响应，图片通过其他方式处理
                image_urls=[],
                is_admin=False,
                reply_to=reply_to,
                count_towards_summary=False,
            )

            # 缓存到内存，防止on_decorating_result重复记录
            if origin not in self._bot_message_cache:
                self._bot_message_cache[origin] = {}
            self._bot_message_cache[origin][message_id] = bot_id

            # 标记为已处理
            self._pending_bot_messages[origin] = (message_id, clean_content)

            self._log_debug(f"记录Bot消息: {clean_content[:50]}...")

        except Exception as e:
            logger.error(f"[MoreChatPlus] on_llm_response记录失败: {e}", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前处理 [at:QQ号] 标签，并记录Bot发送的消息（备用）"""
        if not self.config.core.enable:
            return

        try:
            sender_id = str(event.get_sender_id() or "")
            bot_id = str(self.config.core.bot_qq_id or "")

            # 检查是否已经在on_llm_response中记录过（通过内容匹配）
            origin = event.unified_msg_origin
            message_chain = event.get_messages()
            current_content = ""

            if message_chain:
                for comp in message_chain:
                    if isinstance(comp, Plain):
                        current_content += comp.text

            # 如果最近刚通过on_llm_response记录过相同内容，跳过
            if origin in self._pending_bot_messages:
                _, recorded_content = self._pending_bot_messages[origin]
                if recorded_content in current_content or current_content in recorded_content:
                    self._log_debug("decorating_result: 消息已记录，跳过")
                    # 清除待处理标记
                    del self._pending_bot_messages[origin]
                    # 但仍需处理at标签转换
                    await self._process_at_tags(event)
                    return

            is_bot = sender_id == bot_id

            # 记录Bot发送的消息（如果on_llm_response未捕获到）
            if is_bot and bot_id:
                await self._record_bot_message(event)
                return

            # 处理非Bot消息的at标签转换
            await self._process_at_tags(event)

        except Exception as e:
            logger.error(f"[MoreChatPlus] on_decorating_result 失败: {e}", exc_info=True)

    async def _process_at_tags(self, event: AstrMessageEvent):
        """处理消息中的at标签转换"""
        try:
            message_chain = event.get_messages()
            if not message_chain:
                return

            new_chain = []
            has_at = False
            for comp in message_chain:
                if isinstance(comp, Plain):
                    text = comp.text
                    # 检查是否包含[at:QQ号]格式
                    if '[at:' in text:
                        converted = convert_at_tags_to_components(text)
                        new_chain.extend(converted)
                        has_at = True
                    else:
                        new_chain.append(comp)
                else:
                    new_chain.append(comp)

            if has_at and new_chain:
                event.message_obj.message = new_chain
                self._log_debug("已处理at标签转换")
        except Exception as e:
            logger.error(f"[MoreChatPlus] 处理at标签失败: {e}")

    async def _record_bot_message(self, event: AstrMessageEvent):
        """记录Bot发送的消息到数据库（on_decorating_result备用方案）"""
        try:
            origin = event.unified_msg_origin
            message_id = str(event.message_obj.message_id or "")
            sender_id = str(event.get_sender_id() or "")
            nickname = event.message_obj.sender.nickname if event.message_obj.sender else self.config.core.bot_name
            if not nickname:
                nickname = self.config.core.bot_name

            # 如果message_id为空，生成临时ID
            if not message_id:
                message_id = f"bot_dec_{uuid.uuid4().hex[:8]}"

            # 提取内容
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
                elif isinstance(comp, Reply):
                    reply_to = str(comp.id or "")

            content = " ".join(content_parts).strip()

            # 检查是否已存在（避免重复记录）
            if origin in self._bot_message_cache and message_id in self._bot_message_cache[origin]:
                self._log_debug(f"Bot消息已存在: {message_id}")
                return

            # 保存到数据库
            self.context_manager.add_message(
                origin=origin,
                message_id=message_id,
                user_id=sender_id,
                nickname=nickname,
                content=content,
                has_image=has_image,
                image_urls=image_urls,
                is_admin=False,
                reply_to=reply_to,
                count_towards_summary=False,
            )

            # 缓存到内存
            if origin not in self._bot_message_cache:
                self._bot_message_cache[origin] = {}
            self._bot_message_cache[origin][message_id] = sender_id

            self._log_debug(f"记录Bot消息(备用): {content[:50]}...")

        except Exception as e:
            logger.error(f"[MoreChatPlus] 记录Bot消息失败: {e}", exc_info=True)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理所有消息"""
        if not self.config.core.enable:
            return

        # 提取基本信息用于日志
        raw_msg = event.message_str or ""
        sender_id = str(event.get_sender_id() or "")
        bot_id = str(self.config.core.bot_qq_id or "")
        is_bot = sender_id == bot_id

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
        is_bot_self = user_id == bot_id

        self._log_debug(f"解析: {nickname}({user_id}), is_bot={is_bot_self}")

        # 检查是否是管理员
        is_admin = user_id == self.config.core.admin_user_id

        # 添加到上下文（所有消息都记录，包括Bot的）
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
            count_towards_summary=not is_bot_self,
        )

        # 如果是Bot消息，缓存ID并返回（不触发回复流程）
        if is_bot_self:
            if origin not in self._bot_message_cache:
                self._bot_message_cache[origin] = {}
            self._bot_message_cache[origin][message_id] = user_id
            self._log_debug(f"缓存Bot消息: {message_id}")
            event._morechatplus_processed = True
            return

        # 检查是否是@bot
        is_mentioned = bot_id and f"[at:{bot_id}]" in content

        # 检查触发词
        should_reply = should_trigger_reply(
            content,
            self.config.core.bot_name,
            bot_id,
            self.config.core.trigger_words,
        )

        # 检查是否引用了bot的消息
        if reply_to:
            is_quote_bot = await self._check_quote_is_bot(origin, reply_to)
            if is_quote_bot:
                should_reply = True
                is_mentioned = True
                logger.info(f"[MoreChatPlus] 检测到引用Bot消息，强制触发回复")

        logger.debug(f"[MoreChatPlus] 决策: is_mentioned={is_mentioned}, should_reply={should_reply}")

        # 识图处理
        vision_result = None
        if should_reply and has_image:
            vision_result = await self._process_vision(image_urls, image_ids, event)

        # 触发总结
        if self.context_manager.should_trigger_summary(origin):
            asyncio.create_task(self._trigger_summary(origin))

        # 检查主动回复
        active_reply_info = self._check_pending_active_reply(origin)

        if not (should_reply or active_reply_info):
            self._log_debug("无需回复")
            event._morechatplus_processed = True
            return

        # 构建增强prompt
        enhanced_prompt = await self._build_enhanced_prompt(
            event=event,
            original_content=content,
            user_id=user_id,
            nickname=nickname,
            message_id=message_id,
            vision_result=vision_result,
            active_reply_info=active_reply_info,
            is_mentioned=is_mentioned,
        )

        event._morechatplus_enhanced_prompt = enhanced_prompt
        event._morechatplus_processed = True

        # 非@触发时确保事件传播
        if should_reply and not is_mentioned:
            self._log_debug("非@触发，确保LLM调用")
            if hasattr(event, 'continue_event'):
                event.continue_event()

    async def _check_quote_is_bot(self, origin: str, reply_to: str) -> bool:
        """检查引用的消息是否是Bot发送的"""
        try:
            # 1. 先查数据库
            replied_msg = self.context_manager.get_message_by_id(origin, reply_to)
            if replied_msg:
                return replied_msg.user_id == str(self.config.core.bot_qq_id)

            # 2. 查内存缓存
            if origin in self._bot_message_cache:
                if reply_to in self._bot_message_cache[origin]:
                    return self._bot_message_cache[origin][reply_to] == str(self.config.core.bot_qq_id)

            return False
        except Exception as e:
            logger.error(f"[MoreChatPlus] 检查引用失败: {e}")
            return False

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在LLM请求前注入上下文"""
        if not self.config.core.enable:
            return
        if not hasattr(event, '_morechatplus_enhanced_prompt'):
            return

        enhanced_prompt = event._morechatplus_enhanced_prompt
        injected = False

        try:
            if hasattr(req, 'prompt'):
                req.prompt = enhanced_prompt
                injected = True
            elif hasattr(req, 'messages') and isinstance(req.messages, list):
                for i in range(len(req.messages) - 1, -1, -1):
                    if isinstance(req.messages[i], dict) and req.messages[i].get('role') == 'user':
                        req.messages[i]['content'] = enhanced_prompt
                        injected = True
                        break
            elif hasattr(req, 'contexts') and isinstance(req.contexts, list):
                for i in range(len(req.contexts) - 1, -1, -1):
                    if isinstance(req.contexts[i], dict) and req.contexts[i].get('role') == 'user':
                        req.contexts[i]['content'] = enhanced_prompt
                        injected = True
                        break

            if injected:
                if hasattr(event, 'message_str'):
                    event.message_str = enhanced_prompt
                if hasattr(req, 'contexts'):
                    req.contexts = []
                delattr(event, '_morechatplus_enhanced_prompt')
                self._log_debug("上下文注入成功")
            else:
                logger.error("[MoreChatPlus] 注入失败")
        except Exception as e:
            logger.error(f"[MoreChatPlus] 注入异常: {e}")

    async def _extract_message_info(
        self,
        event: AstrMessageEvent,
    ) -> Optional[tuple]:
        """提取消息信息"""
        try:
            message_id = str(event.message_obj.message_id or "")
            user_id = str(event.get_sender_id() or "")
            nickname = event.message_obj.sender.nickname if event.message_obj.sender else "未知"
            if not nickname:
                nickname = "未知"

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
                        temp_path = await self._resolve_image_to_local(url)
                        if temp_path and Path(temp_path).exists():
                            img_id, exists, final_path = self.image_cache.save_image(url, temp_path)
                            image_ids.append(img_id)
                            content_parts.append(f"[image:{len(image_urls)}:{img_id}]")
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
        """处理图片识别"""
        if not image_urls or not image_ids:
            return ""

        for img_id in image_ids:
            cached_result = self.image_cache.get_vision_result(img_id)
            if cached_result:
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
                return ""

            image_id = image_ids[0]
            local_path = self.image_cache.get_local_path(image_id)

            if not local_path or not Path(local_path).exists():
                return ""

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
            self.image_cache.set_vision_result(image_id, result)
            self.image_cache.increment_send_count(image_id)
            return result

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
                return ""
        return ""

    async def _trigger_summary(self, origin: str):
        """触发上下文总结"""
        if not self.config.active_reply.enable:
            self.context_manager.reset_counter(origin)
            return

        self.context_manager.set_summarizing(origin, True)
        try:
            result = await self.model_a_processor.process_context(origin)
            if result:
                self.context_manager.reset_counter(origin)
                if result.should_reply and result.reply_target_msg_id:
                    self._pending_active_replies[origin] = result
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
        is_mentioned: bool = False,
    ) -> str:
        """构建增强后的prompt"""
        origin = event.unified_msg_origin

        # 最新消息
        latest_message_info = self.context_manager.get_new_message_info(origin, message_id)
        if latest_message_info:
            latest_message = latest_message_info[0]
        else:
            latest_message = format_context_message(
                nickname=nickname,
                user_id=user_id,
                timestamp=time.time(),
                message_id=message_id,
                content=original_content,
                is_admin=user_id == self.config.core.admin_user_id,
                reply_to=None,
            )

        if vision_result:
            latest_message += f"\n[图片识别结果: {vision_result}]"

        # 最近10条
        recent_10_messages = self.context_manager.get_recent_messages_formatted(
            origin=origin,
            limit=10,
            exclude_message_id=message_id
        )

        # 话题总结
        topic_summary = "暂无话题总结"
        recent_summaries = self.db.get_recent_summaries(origin, limit=1)
        if recent_summaries:
            from .model_a_processor import SummaryResult
            summary_obj = SummaryResult(
                summary=recent_summaries[0].summary,
                topic_analysis=recent_summaries[0].topic_analysis,
                suggestions=recent_summaries[0].suggestions,
                should_reply=recent_summaries[0].should_reply,
                timestamp=recent_summaries[0].timestamp,
            )
            topic_summary = self.model_a_processor.format_summary_for_display(summary_obj)

        # 历史上下文
        historical_context = self.context_manager.get_formatted_context(
            origin=origin,
            limit=self.config.context.max_context_messages - 10,
            include_summaries=False,
            exclude_message_ids=[message_id]
        )

        # 系统提示词
        system_prompt_template = self.context_manager.build_system_prompt(
            origin=origin,
            is_mentioned=is_mentioned,
            current_user_id=user_id,
            current_message_id=message_id,
        )

        enhanced_prompt = system_prompt_template.format(
            latest_message=latest_message,
            recent_messages=recent_10_messages,
            topic_summary=topic_summary,
            historical_context=historical_context
        )

        # 用户画像
        user_profile = self.user_profile_manager.get_profile_summary(user_id, origin)
        if user_profile:
            enhanced_prompt += f"\n\n【发送者画像】{user_profile}"

        enhanced_prompt += "\n\n【重要】[at]组件请放在回复开头"

        if user_id == self.config.core.admin_user_id:
            enhanced_prompt += "\n\n【重要】这条消息来自管理员，请特别注意。"
        else:
            enhanced_prompt += "\n\n【重要】这条消息不来自管理员，对方不是你的主人，请特别注意。"

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