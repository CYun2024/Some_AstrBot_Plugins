"""数据库管理模块 - 管理表情包统计、哈气统计和复读记录"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
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


@dataclass
class DailyHaqiRecord:
    """每日哈气统计记录"""
    id: int
    origin: str
    user_id: str
    nickname: str
    text_count: int
    meme_count: int
    date: str
    timestamp: float


@dataclass
class UserInfoRecord:
    """用户信息记录"""
    id: int
    origin: str
    user_id: str
    nickname: str
    last_updated: float


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()
        self._migrate_db()

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

            # 新增：每日哈气统计表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_haqi_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    text_count INTEGER DEFAULT 0,
                    meme_count INTEGER DEFAULT 0,
                    date TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    UNIQUE(origin, user_id, date)
                )
            """)

            # 新增：用户信息表（存储qqid和昵称对应关系）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    last_updated REAL NOT NULL,
                    UNIQUE(origin, user_id)
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
            # 新增索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_haqi_origin_date 
                ON daily_haqi_stats(origin, date DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_haqi_user_date 
                ON daily_haqi_stats(origin, user_id, date)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_info_origin_user 
                ON user_info(origin, user_id)
            """)

            conn.commit()
            logger.info(f"[ChanganCat] 数据库初始化完成: {self.db_path}")

    def _migrate_db(self) -> None:
        """数据库迁移：修复表结构"""
        try:
            with self._lock, self._connect() as conn:
                # 检查 daily_haqi_stats 表的列
                cursor = conn.execute("PRAGMA table_info(daily_haqi_stats)")
                columns = [row[1] for row in cursor.fetchall()]

                # 添加缺失的 timestamp 列
                if 'timestamp' not in columns:
                    logger.info("[ChanganCat] 迁移数据库：添加 timestamp 列到 daily_haqi_stats 表")
                    conn.execute("ALTER TABLE daily_haqi_stats ADD COLUMN timestamp REAL DEFAULT 0")
                    conn.commit()
                    logger.info("[ChanganCat] 数据库迁移完成")

                # 删除不需要的 created_at 列（如果存在）
                # SQLite 不支持直接删除列，需要重建表
                if 'created_at' in columns:
                    logger.info("[ChanganCat] 迁移数据库：移除 created_at 列")
                    self._recreate_daily_haqi_table(conn)

        except Exception as e:
            logger.error(f"[ChanganCat] 数据库迁移失败: {e}")

    def _recreate_daily_haqi_table(self, conn: sqlite3.Connection) -> None:
        """重建 daily_haqi_stats 表（移除 created_at 列）"""
        try:
            # 1. 创建新表
            conn.execute("""
                CREATE TABLE daily_haqi_stats_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    text_count INTEGER DEFAULT 0,
                    meme_count INTEGER DEFAULT 0,
                    date TEXT NOT NULL,
                    timestamp REAL NOT NULL DEFAULT 0,
                    UNIQUE(origin, user_id, date)
                )
            """)

            # 2. 复制数据（排除 created_at 列）
            conn.execute("""
                INSERT INTO daily_haqi_stats_new 
                (id, origin, user_id, nickname, text_count, meme_count, date, timestamp)
                SELECT id, origin, user_id, nickname, text_count, meme_count, date, 
                       COALESCE(timestamp, 0) as timestamp
                FROM daily_haqi_stats
            """)

            # 3. 删除旧表
            conn.execute("DROP TABLE daily_haqi_stats")

            # 4. 重命名新表
            conn.execute("ALTER TABLE daily_haqi_stats_new RENAME TO daily_haqi_stats")

            # 5. 重建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_haqi_origin_date 
                ON daily_haqi_stats(origin, date DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_haqi_user_date 
                ON daily_haqi_stats(origin, user_id, date)
            """)

            conn.commit()
            logger.info("[ChanganCat] 表重建完成，已移除 created_at 列")
        except Exception as e:
            logger.error(f"[ChanganCat] 重建表失败: {e}")
            raise

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

    # ==================== 新增：每日哈气统计操作 ====================

    def save_daily_haqi_stats(self, origin: str, user_id: str, nickname: str,
                           text_count: int, meme_count: int, date: str) -> bool:
        """保存或更新每日哈气统计（带3天覆盖策略）

        策略：
        - 如果日期在3天内（今天、昨天、前天）：覆盖更新
        - 如果日期在3天外：仅插入不更新（保留历史数据）

        Returns:
            是否成功保存
        """
        timestamp = time.time()

        # 计算日期差（今天 - 目标日期）
        try:
            today = datetime.now().strftime("%Y/%m/%d")
            date_obj = datetime.strptime(date, "%Y/%m/%d")
            today_obj = datetime.strptime(today, "%Y/%m/%d")
            days_diff = (today_obj - date_obj).days
        except:
            days_diff = 0

        try:
            with self._lock, self._connect() as conn:
                # 检查是否已存在
                row = conn.execute(
                    "SELECT id FROM daily_haqi_stats WHERE origin = ? AND user_id = ? AND date = ?",
                    (origin, user_id, date)
                ).fetchone()

                if row:
                    # 3天内的数据才覆盖
                    if days_diff <= 3:
                        conn.execute("""
                            UPDATE daily_haqi_stats 
                            SET nickname = ?, text_count = ?, meme_count = ?, timestamp = ?
                            WHERE id = ?
                        """, (nickname, text_count, meme_count, timestamp, row["id"]))
                        logger.debug(f"[ChanganCat] 覆盖更新哈气记录: {user_id} on {date} ({days_diff}天前)")
                    else:
                        # 3天外不覆盖，跳过
                        logger.debug(f"[ChanganCat] 跳过3天外数据: {user_id} on {date} ({days_diff}天前)")
                        return True
                else:
                    # 不存在则插入（无论是否3天内都插入）
                    conn.execute("""
                        INSERT INTO daily_haqi_stats 
                        (origin, user_id, nickname, text_count, meme_count, date, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (origin, user_id, nickname, text_count, meme_count, date, timestamp))
                    logger.debug(f"[ChanganCat] 新增哈气记录: {user_id} on {date}")

                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ChanganCat] 保存每日哈气统计失败: {e}")
            return False

    def save_daily_haqi_stats_batch(self, origin: str, date_stats: Dict[str, List[Tuple]], 
                                    max_override_days: int = 3) -> Dict[str, int]:
        """批量保存多日哈气统计（支持3天覆盖策略）

        Args:
            origin: 群origin
            date_stats: {日期: [(user_id, nickname, text_count, meme_count, total_count), ...]}
            max_override_days: 最大覆盖天数（默认3天）

        Returns:
            {日期: 保存记录数}
        """
        today = datetime.now().strftime("%Y/%m/%d")
        today_obj = datetime.strptime(today, "%Y/%m/%d")

        result = {}

        for date_str, ranking in date_stats.items():
            # 计算天数差
            try:
                date_obj = datetime.strptime(date_str, "%Y/%m/%d")
                days_diff = (today_obj - date_obj).days
            except:
                days_diff = 99  # 解析失败视为久远数据

            saved_count = 0
            for user_id, nickname, text_c, meme_c, total_c in ranking:
                # 保存用户昵称
                self.save_or_update_user_info(origin, user_id, nickname)

                # 保存数据（内部会判断3天策略）
                success = self.save_daily_haqi_stats_with_policy(
                    origin, user_id, nickname, text_c, meme_c, date_str, days_diff, max_override_days
                )
                if success:
                    saved_count += 1

            result[date_str] = saved_count
            logger.info(f"[ChanganCat] 已保存 {date_str} ({days_diff}天前) 的 {saved_count} 条记录")

        return result

    def save_daily_haqi_stats_with_policy(self, origin: str, user_id: str, nickname: str,
                                         text_count: int, meme_count: int, date: str,
                                         days_diff: int, max_override_days: int = 3) -> bool:
        """根据策略保存单日哈气统计（内部方法）"""
        timestamp = time.time()

        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM daily_haqi_stats WHERE origin = ? AND user_id = ? AND date = ?",
                    (origin, user_id, date)
                ).fetchone()

                if row:
                    # 在覆盖天数内的才更新
                    if days_diff <= max_override_days:
                        conn.execute("""
                            UPDATE daily_haqi_stats 
                            SET nickname = ?, text_count = ?, meme_count = ?, timestamp = ?
                            WHERE id = ?
                        """, (nickname, text_count, meme_count, timestamp, row["id"]))
                    else:
                        return True  # 跳过但不报错
                else:
                    # 新记录直接插入
                    conn.execute("""
                        INSERT INTO daily_haqi_stats 
                        (origin, user_id, nickname, text_count, meme_count, date, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (origin, user_id, nickname, text_count, meme_count, date, timestamp))

                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ChanganCat] 保存哈气统计失败: {e}")
            return False

    def get_daily_haqi_stats(self, origin: str, date: str) -> List[DailyHaqiRecord]:
        """获取指定日期的哈气统计"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM daily_haqi_stats 
                    WHERE origin = ? AND date = ?
                    ORDER BY text_count + meme_count DESC
                """, (origin, date)).fetchall()

                return [
                    DailyHaqiRecord(
                        id=row["id"],
                        origin=row["origin"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        text_count=row["text_count"],
                        meme_count=row["meme_count"],
                        date=row["date"],
                        timestamp=row["timestamp"]
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[ChanganCat] 获取每日哈气统计失败: {e}")
            return []

    def get_daily_haqi_stats_range(self, origin: str, start_date: str, end_date: str) -> List[DailyHaqiRecord]:
        """获取指定日期范围的哈气统计"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM daily_haqi_stats 
                    WHERE origin = ? AND date >= ? AND date <= ?
                    ORDER BY date DESC, text_count + meme_count DESC
                """, (origin, start_date, end_date)).fetchall()

                return [
                    DailyHaqiRecord(
                        id=row["id"],
                        origin=row["origin"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        text_count=row["text_count"],
                        meme_count=row["meme_count"],
                        date=row["date"],
                        timestamp=row["timestamp"]
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[ChanganCat] 获取日期范围哈气统计失败: {e}")
            return []

    def get_user_haqi_trend(self, origin: str, user_id: str, days: int = 7) -> List[DailyHaqiRecord]:
        """获取用户最近N天的哈气趋势数据"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM daily_haqi_stats 
                    WHERE origin = ? AND user_id = ?
                    ORDER BY date DESC
                    LIMIT ?
                """, (origin, user_id, days)).fetchall()

                return [
                    DailyHaqiRecord(
                        id=row["id"],
                        origin=row["origin"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        text_count=row["text_count"],
                        meme_count=row["meme_count"],
                        date=row["date"],
                        timestamp=row["timestamp"]
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[ChanganCat] 获取用户哈气趋势失败: {e}")
            return []

    # ==================== 新增：用户信息操作 ====================

    def save_or_update_user_info(self, origin: str, user_id: str, nickname: str) -> Tuple[bool, bool]:
        """保存或更新用户信息

        Returns:
            (success, is_updated): 是否成功，是否是更新操作（False表示新增）
        """
        timestamp = time.time()
        try:
            with self._lock, self._connect() as conn:
                # 检查是否已存在
                row = conn.execute(
                    "SELECT id, nickname FROM user_info WHERE origin = ? AND user_id = ?",
                    (origin, user_id)
                ).fetchone()

                if row:
                    # 检查昵称是否有变化
                    if row["nickname"] != nickname:
                        conn.execute("""
                            UPDATE user_info 
                            SET nickname = ?, last_updated = ?
                            WHERE id = ?
                        """, (nickname, timestamp, row["id"]))
                        conn.commit()
                        logger.info(f"[ChanganCat] 更新用户昵称: {user_id} {row['nickname']} -> {nickname}")
                        return True, True
                    else:
                        # 昵称没变，只更新时间戳
                        conn.execute(
                            "UPDATE user_info SET last_updated = ? WHERE id = ?",
                            (timestamp, row["id"])
                        )
                        conn.commit()
                        return True, False
                else:
                    # 插入新记录
                    conn.execute("""
                        INSERT INTO user_info 
                        (origin, user_id, nickname, last_updated)
                        VALUES (?, ?, ?, ?)
                    """, (origin, user_id, nickname, timestamp))
                    conn.commit()
                    logger.info(f"[ChanganCat] 新增用户信息: {user_id} = {nickname}")
                    return True, False
        except Exception as e:
            logger.error(f"[ChanganCat] 保存用户信息失败: {e}")
            return False, False

    def get_user_nickname(self, origin: str, user_id: str) -> Optional[str]:
        """获取用户昵称"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT nickname FROM user_info WHERE origin = ? AND user_id = ?",
                    (origin, user_id)
                ).fetchone()
                return row["nickname"] if row else None
        except Exception as e:
            logger.error(f"[ChanganCat] 获取用户昵称失败: {e}")
            return None

    def get_user_info(self, origin: str, user_id: str) -> Optional[UserInfoRecord]:
        """获取用户完整信息"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM user_info WHERE origin = ? AND user_id = ?",
                    (origin, user_id)
                ).fetchone()

                if row:
                    return UserInfoRecord(
                        id=row["id"],
                        origin=row["origin"],
                        user_id=row["user_id"],
                        nickname=row["nickname"],
                        last_updated=row["last_updated"]
                    )
                return None
        except Exception as e:
            logger.error(f"[ChanganCat] 获取用户信息失败: {e}")
            return None