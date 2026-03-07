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
        count_towards_summary: bool = True,  # 新增：是否计入总结计数
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
        include_summaries: bool = True,
    ) -> str:
        """获取格式化的上下文"""
        if limit is None:
            limit = self.config.context.max_context_messages

        # 获取最近的总结
        summary_text = ""
        if include_summaries:
            summaries = self.db.get_recent_summaries(origin, limit=5)
            if summaries:
                summary_parts = []
                for s in summaries:
                    time_str = datetime.fromtimestamp(s.timestamp).strftime("%m-%d %H:%M")
                    summary_parts.append(f"[{time_str}] 话题总结: {s.summary}")
                summary_text = "=== 历史话题总结 ===\n" + "\n".join(summary_parts) + "\n=== 当前对话 ===\n"

        # 获取消息
        messages = self.db.get_messages(origin, limit=limit)

        if not messages:
            return summary_text + "(暂无消息记录)"

        # 格式化消息
        formatted_lines = []
        for msg in reversed(messages):  # 按时间顺序
            time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")

            # 管理员标记
            admin_mark = "[管理员]" if msg.is_admin else ""

            # 构建消息头
            header = f"[{msg.nickname}|{msg.user_id}(user_id)|{time_str}]:(#msg{msg.message_id}){admin_mark}"

            # 引用信息
            reply_part = ""
            if msg.reply_to:
                reply_part = f" <引用信息: #msg{msg.reply_to}>"

            # 内容
            content = msg.content

            formatted_lines.append(f"{header}{reply_part} {content}")

        return summary_text + "\n".join(formatted_lines)

    def get_context_for_model_a(self, origin: str) -> str:
        """获取给模型A的上下文"""
        limit = self.config.context.model_a_context_messages
        return self.get_formatted_context(origin, limit=limit)

    def get_new_message_info(
        self,
        origin: str,
        message_id: str,
    ) -> Optional[Tuple[str, str, str]]:
        """获取新消息的信息"""
        messages = self.db.get_messages(origin, limit=1)
        if not messages:
            return None

        msg = messages[0]
        if msg.message_id != message_id:
            # 从缓存中找
            for cached in reversed(self._message_cache[origin]):
                if cached.message_id == message_id:
                    msg = cached
                    break
            else:
                return None

        time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
        admin_mark = "[管理员]" if msg.is_admin else ""

        header = f"[{msg.nickname}|{msg.user_id}(user_id)|{time_str}]:(#msg{msg.message_id}){admin_mark}"

        reply_part = ""
        if msg.reply_to:
            reply_part = f" <引用信息: #msg{msg.reply_to}>"

        formatted = f"{header}{reply_part} {msg.content}"

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

        header = f"[{msg.nickname}|{msg.user_id}(user_id)|{time_str}]:(#msg{msg.message_id}){admin_mark}"

        reply_part = ""
        if msg.reply_to:
            reply_part = f" <引用信息: #msg{msg.reply_to}>"

        return f"{header}{reply_part} {msg.content}"

    def build_system_prompt(self, origin: str, is_new_topic_hint: bool = True) -> str:
        """构建系统提示词"""
        lines = [
            "你现在处于一个QQ群聊中。",
            "",
            "## 消息格式说明",
            "每条消息的格式为：[昵称|user_id|时间]:(消息ID)[管理员标记] <引用信息> 内容",
            "例如：[虹猫猫|28196593|19:20:05]:(#msg267518526) <引用信息: #msg267518526> [at:机巧猫] 可爱喵~",
            "",
            "## 可用标签",
            "- [at:QQ号] - 表示@某人",
            "- [image:ID] - 表示图片",
            "- <引用信息: 消息ID> - 表示回复了某条消息",
            "",
        ]

        if is_new_topic_hint:
            lines.extend([
                "## 重要提示",
                "这可能是一个新的话题，也可能是之前话题的延续。",
                "请仔细分析上下文，确认话题的连续性。",
                "如果是新话题，可以直接回复；如果是延续，请注意承接上文。",
                "",
            ])

        lines.extend([
            "## 回复格式",
            "在回复开头使用 [at:QQ号] 来@你想回复的人",
            "如果你想引用某条消息，在回复开头使用 <引用:消息ID>",
            "例如：[at:123456] <引用:267518526> 你的回复内容",
        ])

        return "\n".join(lines)