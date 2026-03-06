"""用户画像管理模块"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .database import DatabaseManager, UserProfile
from .plugin_config import PluginConfig


class UserProfileManager:
    """用户画像管理器"""

    def __init__(
        self,
        db: DatabaseManager,
        config: PluginConfig,
        context,
    ):
        self.db = db
        self.config = config
        self.context = context
        self._last_update_date: Optional[str] = None
        self._identity_confirmations: Dict[str, Dict] = {}  # 身份确认状态

    async def initialize(self):
        """初始化，启动定时任务"""
        asyncio.create_task(self._daily_update_task())
        logger.info("[MoreChatPlus] 用户画像管理器初始化完成")

    async def _daily_update_task(self):
        """每日更新任务"""
        while True:
            try:
                now = datetime.now()
                target_hour = self.config.user_profile.daily_update_hour

                # 计算到下一个更新时间的等待时间
                if now.hour < target_hour:
                    next_update = now.replace(hour=target_hour, minute=0, second=0)
                else:
                    next_update = (now + timedelta(days=1)).replace(
                        hour=target_hour, minute=0, second=0
                    )

                wait_seconds = (next_update - now).total_seconds()
                logger.info(f"[MoreChatPlus] 下次用户画像更新: {next_update}, 等待 {wait_seconds:.0f} 秒")

                await asyncio.sleep(wait_seconds)

                # 执行更新
                await self._run_daily_update()

            except Exception as e:
                logger.error(f"[MoreChatPlus] 每日更新任务出错: {e}")
                await asyncio.sleep(3600)  # 出错后1小时重试

    async def _run_daily_update(self):
        """执行每日用户画像更新（已修复：实际执行更新逻辑）"""
        logger.info("[MoreChatPlus] 开始每日用户画像更新")

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        try:
            # 获取所有不同的 origin（群聊）
            with self.db._lock, self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT origin FROM messages WHERE timestamp >= ?", 
                    ((datetime.now() - timedelta(days=2)).timestamp(),)
                ).fetchall()
                origins = [row["origin"] for row in rows]
            
            if not origins:
                logger.info("[MoreChatPlus] 没有需要更新的群聊")
                self._last_update_date = yesterday
                return
            
            logger.info(f"[MoreChatPlus] 发现 {len(origins)} 个群聊需要更新用户画像")
            
            # 遍历每个 origin 更新用户画像
            for origin in origins:
                try:
                    await self.update_user_profile_for_origin(origin, yesterday)
                    logger.info(f"[MoreChatPlus] 完成群聊 {origin} 的用户画像更新")
                except Exception as e:
                    logger.error(f"[MoreChatPlus] 更新群聊 {origin} 失败: {e}")
            
            self._last_update_date = yesterday
            logger.info(f"[MoreChatPlus] 每日用户画像更新完成: {yesterday}, 共处理 {len(origins)} 个群聊")

        except Exception as e:
            logger.error(f"[MoreChatPlus] 每日更新执行失败: {e}")
            raise

    async def update_user_profile_for_origin(self, origin: str, date_str: str):
        """更新某个来源的所有用户画像"""
        user_ids = self.db.get_all_user_ids(origin)

        for user_id in user_ids:
            try:
                await self._analyze_user_messages(user_id, origin, date_str)
            except Exception as e:
                logger.error(f"[MoreChatPlus] 分析用户 {user_id} 失败: {e}")

    async def _analyze_user_messages(
        self,
        user_id: str,
        origin: str,
        date_str: str,
    ):
        """分析用户消息并更新画像"""
        messages = self.db.get_user_daily_messages(user_id, origin, date_str)

        if not messages:
            return

        # 限制消息数量
        max_msgs = self.config.user_profile.max_daily_messages
        if len(messages) > max_msgs:
            messages = messages[-max_msgs:]

        # 构建消息文本
        msg_texts = []
        for msg in messages:
            time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M")
            msg_texts.append(f"[{time_str}] {msg.nickname}: {msg.content}")

        messages_str = "\n".join(msg_texts)

        # 调用模型B分析
        analysis = await self._call_model_b_for_profile(user_id, messages_str)

        if analysis:
            # 获取或创建用户画像
            profile = self.db.get_user_profile(user_id, origin)
            if not profile:
                profile = UserProfile(
                    user_id=user_id,
                    origin=origin,
                    nicknames=json.dumps([messages[0].nickname], ensure_ascii=False),
                    personality_traits="",
                    interests="",
                    common_topics="",
                    relationship_with_bot="",
                    last_updated=time.time(),
                    message_count=len(messages),
                    is_verified=False,
                )

            # 更新画像
            profile.personality_traits = analysis.get("personality", profile.personality_traits)
            profile.interests = analysis.get("interests", profile.interests)
            profile.common_topics = analysis.get("topics", profile.common_topics)
            profile.relationship_with_bot = analysis.get("relationship", profile.relationship_with_bot)
            profile.last_updated = time.time()
            profile.message_count += len(messages)

            # 合并昵称
            existing_nicknames = set(json.loads(profile.nicknames or "[]"))
            for msg in messages:
                if msg.nickname:
                    existing_nicknames.add(msg.nickname)
            profile.nicknames = json.dumps(list(existing_nicknames), ensure_ascii=False)

            self.db.save_user_profile(profile)
            logger.info(f"[MoreChatPlus] 更新用户画像: {user_id} @ {origin}")

    async def _call_model_b_for_profile(
        self,
        user_id: str,
        messages_str: str,
    ) -> Optional[Dict]:
        """调用模型B分析用户"""
        try:
            provider_id = self.config.models.model_b_provider
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider()

            if not provider:
                logger.warning("[MoreChatPlus] 模型B提供商不可用")
                return None

            prompt = f"""请分析以下用户的聊天记录，提取用户画像信息。

用户ID: {user_id}

聊天记录:
{messages_str}

请用JSON格式输出分析结果:
{{
    "personality": "性格特点描述",
    "interests": "兴趣爱好",
    "topics": "常聊的话题",
    "relationship": "与机器人的关系（陌生人/普通群友/活跃互动者/朋友等）"
}}

只输出JSON，不要其他内容。"""

            import uuid
            response = await provider.text_chat(
                prompt=prompt,
                session_id=uuid.uuid4().hex,
                persist=False,
            )

            # 解析JSON响应
            text = response.completion_text or ""
            # 尝试提取JSON
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                return json.loads(json_match.group())
            return None

        except Exception as e:
            logger.error(f"[MoreChatPlus] 调用模型B失败: {e}")
            return None

    def get_or_create_profile(
        self,
        user_id: str,
        origin: str,
        nickname: str = "",
    ) -> UserProfile:
        """获取或创建用户画像"""
        profile = self.db.get_user_profile(user_id, origin)

        if not profile:
            profile = UserProfile(
                user_id=user_id,
                origin=origin,
                nicknames=json.dumps([nickname] if nickname else [], ensure_ascii=False),
                personality_traits="",
                interests="",
                common_topics="",
                relationship_with_bot="",
                last_updated=time.time(),
                message_count=0,
                is_verified=False,
            )
            self.db.save_user_profile(profile)

        return profile

    def add_nickname(
        self,
        user_id: str,
        origin: str,
        nickname: str,
    ) -> bool:
        """为用户添加昵称"""
        profile = self.get_or_create_profile(user_id, origin)

        nicknames = json.loads(profile.nicknames or "[]")
        if nickname not in nicknames:
            nicknames.append(nickname)
            profile.nicknames = json.dumps(nicknames, ensure_ascii=False)
            return self.db.save_user_profile(profile)
        return True

    def check_nickname_exists(self, nickname: str, origin: str) -> List[str]:
        """检查昵称是否已存在，返回匹配的用户ID列表"""
        return self.db.find_user_by_nickname(nickname, origin)

    async def check_identity_claim(
        self,
        user_id: str,
        origin: str,
        claimed_name: str,
    ) -> Tuple[bool, str]:
        """检查用户身份声明

        Returns:
            (is_truthful, response_message)
        """
        profile = self.db.get_user_profile(user_id, origin)

        if not profile:
            # 新用户，创建画像
            self.get_or_create_profile(user_id, origin, claimed_name)
            return True, f"欢迎新群友 {claimed_name}~"

        # 检查声明的名字是否在已知昵称中
        known_nicknames = json.loads(profile.nicknames or "[]")

        if claimed_name in known_nicknames:
            return True, ""

        # 可能是冒充或开玩笑
        # 检查是否有其他用户使用了这个名字
        other_users = self.check_nickname_exists(claimed_name, origin)
        other_users = [uid for uid, _ in other_users if uid != user_id]

        if other_users:
            # 确定是冒充
            return False, f"[at:{user_id}] 哈气！你是{claimed_name}？那{other_users[0]}是谁！不许冒充别人！"

        # 可能是新昵称，添加并验证
        self.add_nickname(user_id, origin, claimed_name)
        return True, f"记住你的新称呼啦，{claimed_name}~"

    def get_profile_summary(self, user_id: str, origin: str) -> str:
        """获取用户画像摘要"""
        profile = self.db.get_user_profile(user_id, origin)

        if not profile:
            return ""

        nicknames = json.loads(profile.nicknames or "[]")
        nickname_str = "/".join(nicknames[:3]) if nicknames else "未知"

        parts = [f"昵称: {nickname_str}"]

        if profile.personality_traits:
            parts.append(f"性格: {profile.personality_traits}")
        if profile.interests:
            parts.append(f"兴趣: {profile.interests}")
        if profile.relationship_with_bot:
            parts.append(f"关系: {profile.relationship_with_bot}")

        return " | ".join(parts)

    async def check_new_nickname_reference(
        self,
        nickname: str,
        origin: str,
        context_messages: List[Dict],
    ) -> Optional[str]:
        """检查新昵称是否指向某个用户

        Args:
            nickname: 新出现的昵称
            origin: 消息来源
            context_messages: 上下文消息

        Returns:
            如果确定指向某个用户，返回user_id，否则None
        """
        # 这里应该调用模型A进行判断
        # 简化处理，返回None表示不确定
        return None