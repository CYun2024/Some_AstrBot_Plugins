"""用户画像管理模块（支持模型B备用）"""

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .database import DatabaseManager, UserProfile
from .plugin_config import PluginConfig
from .model_utils import call_model_with_fallback, ModelCallResult


class UserProfileManager:
    """用户画像管理器"""

    def __init__(
        self,
        db: DatabaseManager,
        config: PluginConfig,
        context,
        debugger=None,
    ):
        self.db = db
        self.config = config
        self.context = context
        self.debugger = debugger
        self._last_update_date: Optional[str] = None
        self._identity_confirmations: Dict[str, Dict] = {}

    async def _record_llm_call(self, data: dict):
        """辅助方法：安全上报"""
        if self.debugger and hasattr(self.debugger, 'safe_record_llm_call'):
            await self.debugger.safe_record_llm_call(data)

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

                if now.hour < target_hour:
                    next_update = now.replace(hour=target_hour, minute=0, second=0)
                else:
                    next_update = (now + timedelta(days=1)).replace(
                        hour=target_hour, minute=0, second=0
                    )

                wait_seconds = (next_update - now).total_seconds()
                logger.info(f"[MoreChatPlus] 下次用户画像更新: {next_update}, 等待 {wait_seconds:.0f} 秒")

                await asyncio.sleep(wait_seconds)
                await self._run_daily_update()

            except Exception as e:
                logger.error(f"[MoreChatPlus] 每日更新任务出错: {e}")
                await asyncio.sleep(3600)

    async def _run_daily_update(self):
        """执行每日用户画像更新"""
        logger.info("[MoreChatPlus] 开始每日用户画像更新")

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        try:
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

            for origin in origins:
                try:
                    await self.update_user_profile_for_origin(origin, yesterday)
                    logger.info(f"[MoreChatPlus] 完成群聊 {origin} 的用户画像更新")
                except Exception as e:
                    logger.error(f"[MoreChatPlus] 更新群聊 {origin} 失败: {e}")

            self._last_update_date = yesterday
            logger.info(f"[MoreChatPlus] 每日用户画像更新完成: {yesterday}")

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

        max_msgs = self.config.user_profile.max_daily_messages
        if len(messages) > max_msgs:
            messages = messages[-max_msgs:]

        msg_texts = []
        for msg in messages:
            time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M")
            msg_texts.append(f"[{time_str}] {msg.nickname}: {msg.content}")

        messages_str = "\n".join(msg_texts)

        analysis = await self._call_model_b_for_profile(user_id, messages_str)

        if analysis:
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

            profile.personality_traits = analysis.get("personality", profile.personality_traits)
            profile.interests = analysis.get("interests", profile.interests)
            profile.common_topics = analysis.get("topics", profile.common_topics)
            profile.relationship_with_bot = analysis.get("relationship", profile.relationship_with_bot)
            profile.last_updated = time.time()
            profile.message_count += len(messages)

            existing_nicknames = set(json.loads(profile.nicknames or "[]"))
            for msg in messages:
                if msg.nickname:
                    existing_nicknames.add(msg.nickname)
            profile.nicknames = json.dumps(list(existing_nicknames), ensure_ascii=False)

            self.db.save_user_profile(profile)

            # 记录使用的模型信息
            provider_info = f" (via {analysis.get('_provider', 'unknown')})" if analysis.get('_provider') else ""
            fallback_info = " [备用]" if analysis.get('_used_fallback') else ""
            logger.info(f"[MoreChatPlus] 更新用户画像: {user_id} @ {origin}{provider_info}{fallback_info}")

    async def _call_model_b_for_profile(
        self,
        user_id: str,
        messages_str: str,
    ) -> Optional[Dict]:
        """调用模型B分析用户（支持故障转移）"""
        primary_id = self.config.models.model_b_provider
        fallback_id = self.config.models.model_b_fallback_provider

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

注：792398771是bot自己的QQ号
只输出JSON，不要其他内容。"""

        logger.info(
            f"[MoreChatPlus] 调用模型B分析用户画像 | 用户={user_id} | "
            f"主模型={primary_id or 'default'} | 备用={fallback_id or '无'}"
        )

        result = await call_model_with_fallback(
            context=self.context,
            config=self.config,
            primary_provider_id=primary_id,
            fallback_provider_id=fallback_id,
            prompt=prompt,
            timeout_sec=self.config.timeouts.model_b_sec,
            record_callback=self._record_llm_call,
            purpose="model_b_profile"
        )

        if not result.success:
            logger.error(f"[MoreChatPlus] 模型B调用失败: {result.error}")
            return None

        if result.is_fallback:
            logger.info(f"[MoreChatPlus] 模型B已切换到备用模型: {result.provider_id}")

        try:
            json_match = re.search(r'\{[\s\S]*\}', result.text)
            if json_match:
                analysis = json.loads(json_match.group())
                # 添加元数据供上层记录
                analysis['_provider'] = result.provider_id
                analysis['_used_fallback'] = result.is_fallback
                return analysis
        except json.JSONDecodeError as e:
            logger.error(f"[MoreChatPlus] 解析模型B响应失败: {e}, 响应: {result.text[:200]}")

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
        """检查昵称是否已存在"""
        return self.db.find_user_by_nickname(nickname, origin)

    async def check_identity_claim(
        self,
        user_id: str,
        origin: str,
        claimed_name: str,
    ) -> Tuple[bool, str]:
        """检查用户身份声明"""
        profile = self.db.get_user_profile(user_id, origin)

        if not profile:
            self.get_or_create_profile(user_id, origin, claimed_name)
            return True, f"欢迎新群友 {claimed_name}~"

        known_nicknames = json.loads(profile.nicknames or "[]")

        if claimed_name in known_nicknames:
            return True, ""

        other_users = self.check_nickname_exists(claimed_name, origin)
        other_users = [uid for uid, _ in other_users if uid != user_id]

        if other_users:
            return False, f"[at:{user_id}] 哈气！你是{claimed_name}？那{other_users[0]}是谁！不许冒充别人！"

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
        """检查新昵称是否指向某个用户"""
        return None