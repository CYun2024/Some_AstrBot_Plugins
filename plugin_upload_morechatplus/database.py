"""数据库管理模块 - 管理上下文、用户画像、消息总结"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger


@dataclass
class MessageRecord:
    """消息记录"""
    id: int
    origin: str
    message_id: str
    user_id: str
    nickname: str
    content: str
    timestamp: float
    has_image: bool
    image_urls: str  # JSON list
    is_admin: bool
    reply_to: str  # 回复的消息ID


@dataclass
class ContextSummary:
    """上下文总结"""
    id: int
    origin: str
    start_msg_id: str
    end_msg_id: str
    summary: str
    topic_analysis: str
    suggestions: str
    should_reply: bool
    timestamp: float


@dataclass
class UserProfile:
    """用户画像"""
    user_id: str
    origin: str
    nicknames: str  # JSON list
    personality_traits: str
    interests: str
    common_topics: str
    relationship_with_bot: str
    last_updated: float
    message_count: int
    is_verified: bool  # 是否经过验证


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._lock, self._connect() as conn:
            # 消息记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    has_image INTEGER DEFAULT 0,
                    image_urls TEXT DEFAULT '[]',
                    is_admin INTEGER DEFAULT 0,
                    reply_to TEXT DEFAULT '',
                    UNIQUE(origin, message_id)
                )
            """)

            # 上下文总结表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS context_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    start_msg_id TEXT NOT NULL,
                    end_msg_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    topic_analysis TEXT NOT NULL,
                    suggestions TEXT NOT NULL,
                    should_reply INTEGER DEFAULT 0,
                    timestamp REAL NOT NULL
                )
            """)

            # 用户画像表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    nicknames TEXT DEFAULT '[]',
                    personality_traits TEXT DEFAULT '',
                    interests TEXT DEFAULT '',
                    common_topics TEXT DEFAULT '',
                    relationship_with_bot TEXT DEFAULT '',
                    last_updated REAL DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    is_verified INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, origin)
                )
            """)

            # 昵称映射表（用于快速查找）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nickname_mappings (
                    nickname TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    PRIMARY KEY (nickname, user_id, origin)
                )
            """)

            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_origin_time 
                ON messages(origin, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_user 
                ON messages(user_id, origin)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_origin_msgid 
                ON messages(origin, message_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_summaries_origin 
                ON context_summaries(origin, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nickname_lookup 
                ON nickname_mappings(nickname, origin)
            """)

            conn.commit()
            logger.info(f"[MoreChatPlus] 数据库初始化完成: {self.db_path}")

    # ==================== 消息记录操作 ====================

    def save_message(
        self,
        origin: str,
        message_id: str,
        user_id: str,
        nickname: str,
        content: str,
        timestamp: float = None,
        has_image: bool = False,
        image_urls: List[str] = None,
        is_admin: bool = False,
        reply_to: str = "",
    ) -> bool:
        """保存消息记录"""
        if timestamp is None:
            timestamp = time.time()

        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO messages 
                    (origin, message_id, user_id, nickname, content, timestamp, 
                     has_image, image_urls, is_admin, reply_to)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    origin, message_id, user_id, nickname, content, timestamp,
                    1 if has_image else 0,
                    json.dumps(image_urls or [], ensure_ascii=False),
                    1 if is_admin else 0,
                    reply_to
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[MoreChatPlus] 保存消息失败: {e}")
            return False

    def get_messages(
        self,
        origin: str,
        limit: int = 100,
        before_time: float = None,
        user_id: str = None,
    ) -> List[MessageRecord]:
        """获取消息记录"""
        try:
            with self._lock, self._connect() as conn:
                where_clauses = ["origin = ?"]
                params = [origin]

                if before_time:
                    where_clauses.append("timestamp < ?")
                    params.append(before_time)

                if user_id:
                    where_clauses.append("user_id = ?")
                    params.append(user_id)

                where_sql = " AND ".join(where_clauses)
                params.append(limit)

                rows = conn.execute(f"""
                    SELECT * FROM messages 
                    WHERE {where_sql}
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, params).fetchall()

                return [
                    MessageRecord(
                        id=row["id"],
                        origin=row["origin"],
                        message_id=row["message_id"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        content=row["content"],
                        timestamp=row["timestamp"],
                        has_image=bool(row["has_image"]),
                        image_urls=row["image_urls"],
                        is_admin=bool(row["is_admin"]),
                        reply_to=row["reply_to"],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取消息失败: {e}")
            return []

    def get_messages_by_ids(
        self,
        origin: str,
        message_ids: List[str],
    ) -> List[MessageRecord]:
        """根据ID列表获取消息"""
        if not message_ids:
            return []

        try:
            with self._lock, self._connect() as conn:
                placeholders = ",".join(["?"] * len(message_ids))
                rows = conn.execute(f"""
                    SELECT * FROM messages 
                    WHERE origin = ? AND message_id IN ({placeholders})
                    ORDER BY timestamp DESC
                """, [origin] + message_ids).fetchall()

                return [
                    MessageRecord(
                        id=row["id"],
                        origin=row["origin"],
                        message_id=row["message_id"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        content=row["content"],
                        timestamp=row["timestamp"],
                        has_image=bool(row["has_image"]),
                        image_urls=row["image_urls"],
                        is_admin=bool(row["is_admin"]),
                        reply_to=row["reply_to"],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取消息失败: {e}")
            return []

    def get_message_count(self, origin: str, since: float = None) -> int:
        """获取消息数量"""
        try:
            with self._lock, self._connect() as conn:
                if since:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE origin = ? AND timestamp >= ?",
                        (origin, since)
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE origin = ?",
                        (origin,)
                    ).fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取消息数量失败: {e}")
            return 0

    def cleanup_old_messages(self, origin: str, max_age_days: int) -> int:
        """清理旧消息"""
        cutoff_time = time.time() - (max_age_days * 86400)
        try:
            with self._lock, self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM messages WHERE origin = ? AND timestamp < ?",
                    (origin, cutoff_time)
                )
                conn.commit()
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.info(f"[MoreChatPlus] 清理 {origin} 的 {deleted} 条旧消息")
                return deleted
        except Exception as e:
            logger.error(f"[MoreChatPlus] 清理旧消息失败: {e}")
            return 0

    # ==================== 上下文总结操作 ====================

    def save_summary(
        self,
        origin: str,
        start_msg_id: str,
        end_msg_id: str,
        summary: str,
        topic_analysis: str,
        suggestions: str,
        should_reply: bool,
        timestamp: float = None,
    ) -> bool:
        """保存上下文总结"""
        if timestamp is None:
            timestamp = time.time()

        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT INTO context_summaries 
                    (origin, start_msg_id, end_msg_id, summary, topic_analysis, 
                     suggestions, should_reply, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    origin, start_msg_id, end_msg_id, summary, topic_analysis,
                    suggestions, 1 if should_reply else 0, timestamp
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[MoreChatPlus] 保存总结失败: {e}")
            return False

    def get_recent_summaries(
        self,
        origin: str,
        limit: int = 10,
    ) -> List[ContextSummary]:
        """获取最近的上下文总结"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM context_summaries 
                    WHERE origin = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (origin, limit)).fetchall()

                return [
                    ContextSummary(
                        id=row["id"],
                        origin=row["origin"],
                        start_msg_id=row["start_msg_id"],
                        end_msg_id=row["end_msg_id"],
                        summary=row["summary"],
                        topic_analysis=row["topic_analysis"],
                        suggestions=row["suggestions"],
                        should_reply=bool(row["should_reply"]),
                        timestamp=row["timestamp"],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取总结失败: {e}")
            return []

    # ==================== 用户画像操作 ====================

    def get_user_profile(
        self,
        user_id: str,
        origin: str,
    ) -> Optional[UserProfile]:
        """获取用户画像"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ? AND origin = ?",
                    (user_id, origin)
                ).fetchone()

                if row:
                    return UserProfile(
                        user_id=row["user_id"],
                        origin=row["origin"],
                        nicknames=row["nicknames"],
                        personality_traits=row["personality_traits"],
                        interests=row["interests"],
                        common_topics=row["common_topics"],
                        relationship_with_bot=row["relationship_with_bot"],
                        last_updated=row["last_updated"],
                        message_count=row["message_count"],
                        is_verified=bool(row["is_verified"]),
                    )
                return None
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取用户画像失败: {e}")
            return None

    def save_user_profile(self, profile: UserProfile) -> bool:
        """保存用户画像"""
        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO user_profiles 
                    (user_id, origin, nicknames, personality_traits, interests,
                     common_topics, relationship_with_bot, last_updated, message_count, is_verified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    profile.user_id, profile.origin, profile.nicknames,
                    profile.personality_traits, profile.interests,
                    profile.common_topics, profile.relationship_with_bot,
                    profile.last_updated, profile.message_count,
                    1 if profile.is_verified else 0
                ))
                conn.commit()

                # 更新昵称映射
                self._update_nickname_mappings(conn, profile)

                return True
        except Exception as e:
            logger.error(f"[MoreChatPlus] 保存用户画像失败: {e}")
            return False

    def _update_nickname_mappings(
        self,
        conn: sqlite3.Connection,
        profile: UserProfile,
    ) -> None:
        """更新昵称映射"""
        try:
            nicknames = json.loads(profile.nicknames or "[]")

            # 删除旧映射
            conn.execute(
                "DELETE FROM nickname_mappings WHERE user_id = ? AND origin = ?",
                (profile.user_id, profile.origin)
            )

            # 添加新映射
            for nickname in nicknames:
                if nickname:
                    conn.execute("""
                        INSERT OR REPLACE INTO nickname_mappings 
                        (nickname, user_id, origin, confidence)
                        VALUES (?, ?, ?, ?)
                    """, (nickname.lower(), profile.user_id, profile.origin, 1.0))

            conn.commit()
        except Exception as e:
            logger.error(f"[MoreChatPlus] 更新昵称映射失败: {e}")

    def find_user_by_nickname(
        self,
        nickname: str,
        origin: str,
    ) -> List[Tuple[str, float]]:
        """根据昵称查找用户"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT user_id, confidence FROM nickname_mappings 
                    WHERE nickname LIKE ? AND origin = ?
                """, (f"%{nickname.lower()}%", origin)).fetchall()

                return [(row["user_id"], row["confidence"]) for row in rows]
        except Exception as e:
            logger.error(f"[MoreChatPlus] 查找昵称失败: {e}")
            return []

    def get_user_message_count(
        self,
        user_id: str,
        origin: str,
        since: float = None,
    ) -> int:
        """获取用户消息数量"""
        try:
            with self._lock, self._connect() as conn:
                if since:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ? AND origin = ? AND timestamp >= ?",
                        (user_id, origin, since)
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ? AND origin = ?",
                        (user_id, origin)
                    ).fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取用户消息数量失败: {e}")
            return 0

    def get_user_daily_messages(
        self,
        user_id: str,
        origin: str,
        date_str: str,  # YYYY-MM-DD
    ) -> List[MessageRecord]:
        """获取用户某天的消息"""
        try:
            # 计算时间范围
            import datetime
            date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            start_time = date.timestamp()
            end_time = (date + datetime.timedelta(days=1)).timestamp()

            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM messages 
                    WHERE user_id = ? AND origin = ? AND timestamp >= ? AND timestamp < ?
                    ORDER BY timestamp ASC
                """, (user_id, origin, start_time, end_time)).fetchall()

                return [
                    MessageRecord(
                        id=row["id"],
                        origin=row["origin"],
                        message_id=row["message_id"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        content=row["content"],
                        timestamp=row["timestamp"],
                        has_image=bool(row["has_image"]),
                        image_urls=row["image_urls"],
                        is_admin=bool(row["is_admin"]),
                        reply_to=row["reply_to"],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取用户每日消息失败: {e}")
            return []

    def get_all_user_ids(self, origin: str) -> List[str]:
        """获取所有用户ID"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT user_id FROM messages WHERE origin = ?",
                    (origin,)
                ).fetchall()
                return [row["user_id"] for row in rows]
        except Exception as e:
            logger.error(f"[MoreChatPlus] 获取用户列表失败: {e}")
            return []
