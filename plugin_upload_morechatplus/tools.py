"""LLM工具模块"""
from __future__ import annotations
import json
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class ChatTools:
    """聊天工具集"""

    def __init__(self, db, context_manager, user_profile_manager) -> None:
        self.db = db
        self.context_manager = context_manager
        self.user_profile_manager = user_profile_manager

    async def get_message_content(
        self,
        event: AstrMessageEvent,
        message_id: str,
    ) -> str:
        """获取指定消息ID的完整内容

        Args:
            message_id: 消息ID（如 267518526，不需要#msg前缀）
        """
        origin = event.unified_msg_origin

        msg = self.context_manager.get_message_by_id(origin, message_id)

        if not msg:
            return json.dumps({
                "status": "error",
                "message": f"未找到消息 #{message_id}"
            }, ensure_ascii=False)

        return json.dumps({
            "status": "success",
            "message_id": msg.message_id,
            "user_id": msg.user_id,
            "nickname": msg.nickname,
            "content": msg.content,
            "timestamp": msg.timestamp,
            "has_image": msg.has_image,
        }, ensure_ascii=False, indent=2)

    async def get_user_profile(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> str:
        """获取指定用户的画像信息

        Args:
            user_id: 用户ID（QQ号）
        """
        origin = event.unified_msg_origin

        profile = self.db.get_user_profile(user_id, origin)

        if not profile:
            return json.dumps({
                "status": "not_found",
                "message": f"未找到用户 {user_id} 的画像"
            }, ensure_ascii=False)

        nicknames = json.loads(profile.nicknames or "[]")

        return json.dumps({
            "status": "success",
            "user_id": profile.user_id,
            "nicknames": nicknames,
            "personality_traits": profile.personality_traits,
            "interests": profile.interests,
            "common_topics": profile.common_topics,
            "relationship_with_bot": profile.relationship_with_bot,
            "message_count": profile.message_count,
            "is_verified": profile.is_verified,
        }, ensure_ascii=False, indent=2)

    async def query_nickname(
        self,
        event: AstrMessageEvent,
        nickname: str,
    ) -> str:
        """查询昵称对应的用户

        Args:
            nickname: 要查询的昵称
        """
        origin = event.unified_msg_origin

        results = self.db.find_user_by_nickname(nickname, origin)

        if not results:
            return json.dumps({
                "status": "not_found",
                "message": f"未找到昵称 '{nickname}' 对应的用户"
            }, ensure_ascii=False)

        candidates = []
        for user_id, confidence in results:
            profile = self.db.get_user_profile(user_id, origin)
            if profile:
                nicknames = json.loads(profile.nicknames or "[]")
                candidates.append({
                    "user_id": user_id,
                    "nicknames": nicknames,
                    "confidence": confidence,
                })

        return json.dumps({
            "status": "success",
            "query": nickname,
            "candidates": candidates,
        }, ensure_ascii=False, indent=2)

    async def get_recent_context(
        self,
        event: AstrMessageEvent,
        count: int = 20,
    ) -> str:
        """获取最近的上下文消息

        Args:
            count: 获取的消息数量（默认20条）
        """
        origin = event.unified_msg_origin

        messages = self.db.get_messages(origin, limit=min(count, 50))

        formatted = []
        for msg in reversed(messages):
            from datetime import datetime
            time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M:%S")
            admin_mark = "[管理员]" if msg.is_admin else ""
            formatted.append(
                f"[{msg.nickname}|{msg.user_id}|{time_str}]:(#msg{msg.message_id}){admin_mark} {msg.content}"
            )

        return json.dumps({
            "status": "success",
            "count": len(formatted),
            "context": "\n".join(formatted),
        }, ensure_ascii=False, indent=2)

    async def add_user_nickname(
        self,
        event: AstrMessageEvent,
        user_id: str,
        nickname: str,
    ) -> str:
        """为用户添加新昵称

        Args:
            user_id: 用户ID
            nickname: 新昵称
        """
        origin = event.unified_msg_origin

        success = self.user_profile_manager.add_nickname(user_id, origin, nickname)

        if success:
            return json.dumps({
                "status": "success",
                "message": f"已为 {user_id} 添加昵称 '{nickname}'"
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "status": "error",
                "message": "添加昵称失败"
            }, ensure_ascii=False)
