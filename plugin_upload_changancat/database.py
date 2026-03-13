"""数据库管理模块 - 管理表情包统计、哈气统计和复读记录"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger


@dataclass
class MemeStatRecord:
    """表情包统计记录"""
    id: int
    origin: str
    image_id: str
    image_url: str
    sender_id: str
    sender_nickname: str
    timestamp: float
    use_count: int


@dataclass
class HaqiStatRecord:
    """哈气统计记录"""
    id: int
    origin: str
    user_id: str
    nickname: str
    message_content: str
    timestamp: float


@dataclass
class RepeatRecord:
    """复读记录"""
    id: int
    origin: str
    message_content: str
    message_id: str
    timestamp: float


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
            # 表情包统计表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meme_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    image_id TEXT NOT NULL,
                    image_url TEXT DEFAULT '',
                    sender_id TEXT NOT NULL,
                    sender_nickname TEXT DEFAULT '',
                    timestamp REAL NOT NULL,
                    use_count INTEGER DEFAULT 1,
                    UNIQUE(origin, image_id)
                )
            """)

            # 哈气统计表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS haqi_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    message_content TEXT DEFAULT '',
                    timestamp REAL NOT NULL
                )
            """)

            # 复读记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS repeat_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    message_content TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)

            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_meme_stats_origin_time 
                ON meme_stats(origin, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_meme_stats_image 
                ON meme_stats(origin, image_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_haqi_stats_origin_time 
                ON haqi_stats(origin, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_haqi_stats_user 
                ON haqi_stats(origin, user_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_repeat_records_origin 
                ON repeat_records(origin, timestamp DESC)
            """)

            conn.commit()
            logger.info(f"[ChanganCat] 数据库初始化完成: {self.db_path}")

    # ==================== 表情包统计操作 ====================

    def record_meme(self, origin: str, image_id: str, image_url: str,
                    sender_id: str, sender_nickname: str) -> bool:
        """记录表情包使用"""
        timestamp = time.time()
        try:
            with self._lock, self._connect() as conn:
                # 检查是否已存在
                row = conn.execute(
                    "SELECT id, use_count FROM meme_stats WHERE origin = ? AND image_id = ?",
                    (origin, image_id)
                ).fetchone()

                if row:
                    # 更新使用次数
                    conn.execute(
                        "UPDATE meme_stats SET use_count = use_count + 1, timestamp = ? WHERE id = ?",
                        (timestamp, row["id"])
                    )
                else:
                    # 插入新记录
                    conn.execute("""
                        INSERT INTO meme_stats 
                        (origin, image_id, image_url, sender_id, sender_nickname, timestamp, use_count)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                    """, (origin, image_id, image_url, sender_id, sender_nickname, timestamp))

                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ChanganCat] 记录表情包失败: {e}")
            return False

    def get_top_memes(self, origin: str, since: float, limit: int = 3) -> List[MemeStatRecord]:
        """获取热门表情包"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM meme_stats 
                    WHERE origin = ? AND timestamp >= ?
                    ORDER BY use_count DESC, timestamp DESC
                    LIMIT ?
                """, (origin, since, limit)).fetchall()

                return [
                    MemeStatRecord(
                        id=row["id"],
                        origin=row["origin"],
                        image_id=row["image_id"],
                        image_url=row["image_url"],
                        sender_id=row["sender_id"],
                        sender_nickname=row["sender_nickname"],
                        timestamp=row["timestamp"],
                        use_count=row["use_count"]
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[ChanganCat] 获取热门表情包失败: {e}")
            return []

    def get_meme_by_image_id(self, origin: str, image_id: str) -> Optional[MemeStatRecord]:
        """根据图片ID获取表情包记录"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM meme_stats WHERE origin = ? AND image_id = ?",
                    (origin, image_id)
                ).fetchone()

                if row:
                    return MemeStatRecord(
                        id=row["id"],
                        origin=row["origin"],
                        image_id=row["image_id"],
                        image_url=row["image_url"],
                        sender_id=row["sender_id"],
                        sender_nickname=row["sender_nickname"],
                        timestamp=row["timestamp"],
                        use_count=row["use_count"]
                    )
                return None
        except Exception as e:
            logger.error(f"[ChanganCat] 获取表情包记录失败: {e}")
            return None

    # ==================== 哈气统计操作 ====================

    def record_haqi(self, origin: str, user_id: str, nickname: str,
                    message_content: str) -> bool:
        """记录哈气"""
        timestamp = time.time()
        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT INTO haqi_stats 
                    (origin, user_id, nickname, message_content, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """, (origin, user_id, nickname, message_content, timestamp))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ChanganCat] 记录哈气失败: {e}")
            return False

    def get_haqi_ranking(self, origin: str, since: float) -> List[Tuple[str, str, int]]:
        """获取哈气排行榜 (user_id, nickname, count)"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT user_id, nickname, COUNT(*) as cnt 
                    FROM haqi_stats 
                    WHERE origin = ? AND timestamp >= ?
                    GROUP BY user_id
                    ORDER BY cnt DESC
                """, (origin, since)).fetchall()

                return [(row["user_id"], row["nickname"], row["cnt"]) for row in rows]
        except Exception as e:
            logger.error(f"[ChanganCat] 获取哈气排行榜失败: {e}")
            return []

    def get_user_haqi_count(self, origin: str, user_id: str, since: float) -> int:
        """获取用户哈气次数"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM haqi_stats WHERE origin = ? AND user_id = ? AND timestamp >= ?",
                    (origin, user_id, since)
                ).fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            logger.error(f"[ChanganCat] 获取用户哈气次数失败: {e}")
            return 0

    # ==================== 复读记录操作 ====================

    def record_repeat(self, origin: str, message_content: str, message_id: str) -> bool:
        """记录复读"""
        timestamp = time.time()
        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT INTO repeat_records 
                    (origin, message_content, message_id, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (origin, message_content, message_id, timestamp))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ChanganCat] 记录复读失败: {e}")
            return False

    def has_repeated(self, origin: str, message_content: str, since: float) -> bool:
        """检查是否已经复读过相同内容"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM repeat_records WHERE origin = ? AND message_content = ? AND timestamp >= ?",
                    (origin, message_content, since)
                ).fetchone()
                return row is not None
        except Exception as e:
            logger.error(f"[ChanganCat] 检查复读记录失败: {e}")
            return False

    def cleanup_old_records(self, max_age_days: int) -> int:
        """清理旧记录"""
        cutoff_time = time.time() - (max_age_days * 86400)
        try:
            with self._lock, self._connect() as conn:
                # 清理表情包统计
                cursor1 = conn.execute(
                    "DELETE FROM meme_stats WHERE timestamp < ?",
                    (cutoff_time,)
                )
                # 清理哈气统计
                cursor2 = conn.execute(
                    "DELETE FROM haqi_stats WHERE timestamp < ?",
                    (cutoff_time,)
                )
                # 清理复读记录
                cursor3 = conn.execute(
                    "DELETE FROM repeat_records WHERE timestamp < ?",
                    (cutoff_time,)
                )
                conn.commit()
                total = cursor1.rowcount + cursor2.rowcount + cursor3.rowcount
                if total > 0:
                    logger.info(f"[ChanganCat] 清理了 {total} 条旧记录")
                return total
        except Exception as e:
            logger.error(f"[ChanganCat] 清理旧记录失败: {e}")
            return 0

    # ==================== 消息记录查询（从morechatplus数据库） ====================

    def get_messages_for_stats(self, origin: str, since: float) -> List[Dict]:
        """获取用于统计的消息（通过直接查询morechatplus数据库）"""
        # 这个函数将在main.py中通过访问morechatplus的数据库来实现
        return []