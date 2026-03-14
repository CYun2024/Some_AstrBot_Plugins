"""上下文管理模块"""

import json
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .database import DatabaseManager, MessageRecord
from .plugin_config import PluginConfig


class ContextManager:
    """上下文管理器"""

    def __init__(
        self,
        db: DatabaseManager,
        config: PluginConfig,
    ):
        self.db = db
        self.config = config
        # 内存缓存，用于快速访问最近消息
        self._message_cache: Dict[str, List[MessageRecord]] = defaultdict(list)
        self._message_counter: Dict[str, int] = defaultdict(int)
        # 新增：正在总结中的标记，防止并发触发
        self._summarizing: Dict[str, bool] = defaultdict(bool)

    def add_message(
        self,
        origin: str,
        message_id: str,
        user_id: str,
        nickname: str,
        content: str,
        has_image: bool = False,
        image_urls: List[str] = None,
        is_admin: bool = False,
        reply_to: str = "",
        count_towards_summary: bool = True,
    ) -> MessageRecord:
        """添加消息到上下文"""
        timestamp = time.time()

        # 保存到数据库
        self.db.save_message(
            origin=origin,
            message_id=message_id,
            user_id=user_id,
            nickname=nickname,
            content=content,
            timestamp=timestamp,
            has_image=has_image,
            image_urls=image_urls,
            is_admin=is_admin,
            reply_to=reply_to,
        )

        # 创建消息记录
        record = MessageRecord(
            id=0,  # 数据库会分配
            origin=origin,
            message_id=message_id,
            user_id=user_id,
            nickname=nickname,
            content=content,
            timestamp=timestamp,
            has_image=has_image,
            image_urls=json.dumps(image_urls or [], ensure_ascii=False),
            is_admin=is_admin,
            reply_to=reply_to,
        )

        # 更新缓存
        self._message_cache[origin].append(record)

        # 限制缓存大小
        max_cache = self.config.context.max_context_messages * 2
        if len(self._message_cache[origin]) > max_cache:
            self._message_cache[origin] = self._message_cache[origin][-max_cache:]

        # 只有非 bot 消息才增加计数器
        if count_towards_summary:
            self._message_counter[origin] += 1

        return record

    def get_formatted_context(
        self,
        origin: str,
        limit: int = None,
        include_summaries: bool = False,  # 修改为默认False，新格式中总结单独处理
        exclude_message_ids: List[str] = None,
    ) -> str:
        """获取格式化的上下文（优化历史总结展示）"""
        if limit is None:
            limit = self.config.context.max_context_messages

        # 获取消息，排除指定ID
        messages = self.db.get_messages(origin, limit=limit + (len(exclude_message_ids) if exclude_message_ids else 0))

        if exclude_message_ids:
            messages = [m for m in messages if m.message_id not in exclude_message_ids]
            messages = messages[:limit]

        if not messages:
            return "(暂无消息记录)"

        # 格式化消息
        formatted_lines = []
        for msg in reversed(messages):  # 按时间顺序
            formatted_lines.append(self.format_message_for_llm(msg))

        return "\n".join(formatted_lines)

    def get_recent_messages_formatted(
        self,
        origin: str,
        limit: int = 10,
        exclude_message_id: str = None,
    ) -> str:
        """获取最近N条消息的格式化文本（用于【最近10条消息】）"""
        messages = self.db.get_messages(origin, limit=limit + (1 if exclude_message_id else 0))

        if exclude_message_id:
            messages = [m for m in messages if m.message_id != exclude_message_id]

        messages = messages[:limit]

        if not messages:
            return "(暂无近期消息)"

        formatted_lines = []
        for msg in reversed(messages):  # 按时间顺序，从早到晚
            formatted_lines.append(self.format_message_for_llm(msg))

        return "\n".join(formatted_lines)

    def get_context_for_model_a(self, origin: str) -> str:
        """获取给模型A的上下文"""
        limit = self.config.context.model_a_context_messages
        return self.get_formatted_context(origin, limit=limit, include_summaries=False)

    def get_new_message_info(
        self,
        origin: str,
        message_id: str,
    ) -> Optional[Tuple[str, str, str]]:
        """获取新消息的信息（用于【最新消息】）"""
        # 先从数据库获取最新消息
        messages = self.db.get_messages(origin, limit=5)

        msg = None
        for m in messages:
            if m.message_id == message_id:
                msg = m
                break

        # 如果没找到，从缓存找
        if not msg:
            for cached in reversed(self._message_cache[origin]):
                if cached.message_id == message_id:
                    msg = cached
                    break

        if not msg:
            return None

        formatted = self.format_message_for_llm(msg)
        return formatted, msg.user_id, msg.nickname

    def should_trigger_summary(self, origin: str) -> bool:
        """检查是否应该触发总结（同时检查是否正在总结中）"""
        if self._summarizing[origin]:
            # 如果正在总结中，不触发新总结
            return False
        interval = self.config.context.summary_interval
        return self._message_counter[origin] >= interval

    def reset_counter(self, origin: str):
        """重置计数器"""
        self._message_counter[origin] = 0
        self._summarizing[origin] = False  # 总结完成，清除标记

    def get_message_count_since_summary(self, origin: str) -> int:
        """获取自上次总结以来的消息数"""
        return self._message_counter[origin]

    def set_summarizing(self, origin: str, summarizing: bool = True):
        """设置正在总结中的状态"""
        self._summarizing[origin] = summarizing

    def cleanup_old_context(self, origin: str):
        """清理旧上下文"""
        max_age = self.config.context.context_max_age_days
        deleted = self.db.cleanup_old_messages(origin, max_age)
        if deleted > 0:
            logger.info(f"[MoreChatPlus] 清理 {origin} 的 {deleted} 条旧消息")

    def get_message_by_id(
        self,
        origin: str,
        message_id: str,
    ) -> Optional[MessageRecord]:
        """根据ID获取消息"""
        messages = self.db.get_messages_by_ids(origin, [message_id])
        return messages[0] if messages else None

    def get_messages_with_new_nickname(
        self,
        origin: str,
        nickname: str,
        max_groups: int = 20,
        messages_per_group: int = 5,
    ) -> List[List[MessageRecord]]:
        """获取包含新昵称的消息组"""
        # 从数据库搜索包含该昵称的消息
        all_messages = self.db.get_messages(origin, limit=500)

        matching_groups = []
        for i, msg in enumerate(all_messages):
            if nickname.lower() in msg.content.lower():
                # 找到匹配的消息，收集该消息及其后的消息
                group = [msg]
                for j in range(1, messages_per_group + 1):
                    if i + j < len(all_messages):
                        group.append(all_messages[i + j])
                matching_groups.append(group)

                if len(matching_groups) >= max_groups:
                    break

        return matching_groups

    def format_message_for_llm(self, msg: MessageRecord) -> str:
        """格式化单条消息给LLM"""
        time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
        admin_mark = "[管理员]" if msg.is_admin else ""

        # 格式: [昵称|user_id|时间]:(msg:消息ID)[管理员标记] <引用:消息ID> 内容
        header = f"[{msg.nickname}|{msg.user_id}|{time_str}]:(msg:{msg.message_id}){admin_mark}"

        reply_part = ""
        if msg.reply_to:
            reply_part = f" <引用:{msg.reply_to}>"

        return f"{header}{reply_part} {msg.content}"

    def build_system_prompt(
        self, 
        origin: str, 
        is_mentioned: bool = False,
        current_user_id: str = "",
        current_message_id: str = "",
    ) -> str:
        """构建系统提示词（新格式）"""
        mention_hint = "（对方@了你，请回复这条消息）" if is_mentioned else ""

        lines = [
            f"【最新消息】{mention_hint}",
            "{latest_message}",  # 占位符，由调用者替换
            "",
            "【最近10条消息】",
            "{recent_messages}",  # 占位符
            "",
            "【最近群聊话题】",
            "{topic_summary}",  # 占位符，由模型A总结提供
            "",
            "【历史上下文（仅供回复参考）】",
            "{historical_context}",  # 占位符
            "",
            "【对话理解规则】",
            '1. 严格根据user_id识别人物，不要依赖昵称（昵称可能重复或变化）',
            '2. 关注@提及和回复关系，理解对话链条',
            '3. 注意时间戳，超过5分钟的发言视为历史上下文，当前焦点在最近5分钟',
            '4. 当用户说"你刚才说的"、"他提到的"等代词时，结合上下文准确指代',
            '5. 如果话题切换，请在回复中不要再牵扯旧话题',
            "",
            "【其他规则】",
            f"- 如需@某人，使用[at:QQ号]格式，如果要@发送者，在回复开头使用 [at:{current_user_id}] 来@TA。 如果需要引用这条信息，在开头加上 <引用:{current_message_id}>。",
            "- 如涉及之前的内容，请根据你的上下文记忆回答，不要虚构不存在的事",
            "",
            "## 可用工具",
            "- morechatplus_get_message(message_id) - 获取指定消息的完整内容",
            "- morechatplus_get_image_vision(image_id) - 获取图片的识图结果",
        ]

        return "\n".join(lines)