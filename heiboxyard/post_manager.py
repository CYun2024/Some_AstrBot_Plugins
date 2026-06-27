"""
小黑盒帖子管理模块（重构版）
负责：数据库操作、图片下载、内容解析、帖子入库流程、评论存储
核心改进：
- 新增 post_comments 表存储前三评论，以 link_id + rank 硬绑定
- 增加 comment_time 字段，记录评论发布时间
- 所有 daily_no 变更操作自动级联同步 llm_analyses（通过 link_id 硬绑定）
- 统一 sync 方法，避免数据失联
- 修复重置/调整顺序时的 UNIQUE 约束冲突问题
- 修复 swap_daily_no 按 daily_no 排序，确保序号与编号对应
"""
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from astrbot.api import logger

from .utils import (
    get_current_window, get_current_window_no, ts_to_bj_str,
    get_date_str_from_ts, format_daily_no, parse_daily_no,
    get_window_for_timestamp, get_window_by_no
)


class PostManager:
    """帖子管理器：处理数据库、图片下载、帖子入库、评论存储"""

    def __init__(self, db_path: Path, image_dir: Path, program_path: Path,
                 content_fetch_interval_seconds: int = 30):
        self.db_path = db_path
        self.image_dir = image_dir
        self.program_path = program_path
        self.content_fetch_interval_seconds = content_fetch_interval_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ==================== 数据库初始化 ====================

    def _ensure_db(self):
        """确保数据库表存在"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
            table_exists = cur.fetchone() is not None
            conn.close()
            if not table_exists:
                logger.warning("数据库 posts 表不存在，重新初始化...")
                self._init_db()
        except Exception as e:
            logger.error(f"检查数据库表存在性失败: {e}")
            self._init_db()

    def _ensure_table_schema(self, conn):
        """确保 posts 表包含所有必要字段"""
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(posts)")
        existing_cols = {row[1] for row in cur.fetchall()}

        migrations = []
        if "daily_no" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN daily_no TEXT")
        if "userid" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN userid INTEGER DEFAULT 0")
        if "username" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN username TEXT")
        if "avatar" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN avatar TEXT")
        if "topics" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN topics TEXT")
        if "window_start" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN window_start INTEGER")
        if "source" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN source TEXT DEFAULT 'feed'")
        if "date_str" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN date_str TEXT")
        if "top_comment_count" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN top_comment_count INTEGER DEFAULT 0")

        for sql in migrations:
            try:
                cur.execute(sql)
                logger.info(f"数据库迁移: {sql}")
            except Exception as e:
                logger.warning(f"迁移跳过或失败: {sql} - {e}")

        if migrations:
            conn.commit()
            logger.info("数据库迁移完成")

    def _init_db(self):
        """初始化数据库（重构版：增加 post_comments 表，含 comment_time）"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # posts 表
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
        table_exists = cur.fetchone() is not None

        if not table_exists:
            cur.execute("""
                CREATE TABLE posts (
                    link_id INTEGER PRIMARY KEY,
                    daily_no TEXT,
                    window_start INTEGER,
                    date_str TEXT,
                    title TEXT,
                    create_at INTEGER,
                    userid INTEGER,
                    username TEXT,
                    avatar TEXT,
                    topics TEXT,
                    content TEXT,
                    image_urls TEXT,
                    fetched_at TEXT,
                    source TEXT DEFAULT 'feed',
                    top_comment_count INTEGER DEFAULT 0
                )
            """)
            cur.execute("CREATE INDEX idx_posts_window ON posts(window_start)")
            cur.execute("CREATE INDEX idx_posts_window_no ON posts(window_start, daily_no)")
            cur.execute("CREATE INDEX idx_posts_date ON posts(date_str)")
            logger.info("帖子表初始化完成")
        else:
            self._ensure_table_schema(conn)
            for idx_name, idx_sql in [
                ("idx_posts_window", "CREATE INDEX idx_posts_window ON posts(window_start)"),
                ("idx_posts_window_no", "CREATE INDEX idx_posts_window_no ON posts(window_start, daily_no)"),
                ("idx_posts_date", "CREATE INDEX idx_posts_date ON posts(date_str)"),
            ]:
                cur.execute(f"SELECT name FROM sqlite_master WHERE type='index' AND name='{idx_name}'")
                if not cur.fetchone():
                    try:
                        cur.execute(idx_sql)
                    except Exception as e:
                        logger.warning(f"创建索引失败 {idx_name}: {e}")

        # 新增：post_comments 表（存储前三评论），包含 comment_time
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='post_comments'")
        if not cur.fetchone():
            cur.execute("""
                CREATE TABLE post_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    link_id INTEGER NOT NULL,
                    comment_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL,
                    username TEXT,
                    user_id TEXT,
                    avatar TEXT,
                    text TEXT,
                    up INTEGER DEFAULT 0,
                    has_image BOOLEAN DEFAULT 0,
                    images TEXT,
                    comment_time INTEGER,
                    fetched_at TEXT,
                    UNIQUE(link_id, rank)
                )
            """)
            cur.execute("CREATE INDEX idx_comments_link ON post_comments(link_id)")
            cur.execute("CREATE INDEX idx_comments_rank ON post_comments(link_id, rank)")
            logger.info("评论表 post_comments 初始化完成")
        else:
            # 检查并添加 comment_time 列
            cur.execute("PRAGMA table_info(post_comments)")
            existing_cols = {row[1] for row in cur.fetchall()}
            if "comment_time" not in existing_cols:
                cur.execute("ALTER TABLE post_comments ADD COLUMN comment_time INTEGER")
                logger.info("评论表添加 comment_time 列")
            # 确保索引存在
            for idx_name, idx_sql in [
                ("idx_comments_link", "CREATE INDEX idx_comments_link ON post_comments(link_id)"),
                ("idx_comments_rank", "CREATE INDEX idx_comments_rank ON post_comments(link_id, rank)"),
            ]:
                cur.execute(f"SELECT name FROM sqlite_master WHERE type='index' AND name='{idx_name}'")
                if not cur.fetchone():
                    try:
                        cur.execute(idx_sql)
                    except Exception as e:
                        logger.warning(f"创建索引失败 {idx_name}: {e}")

        conn.commit()
        conn.close()

    # ==================== 评论操作 ====================

    def save_top_comments(self, link_id: int, top_comments: list[dict]):
        """保存帖子前三评论（硬绑定：link_id + rank），包含评论时间"""
        if not top_comments:
            return

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 清理旧数据
            cur.execute("DELETE FROM post_comments WHERE link_id = ?", (link_id,))

            valid_count = 0
            for comment in top_comments:
                rank = comment.get("rank", 0)
                if rank < 1 or rank > 3:
                    continue

                images = comment.get("images", [])
                images_str = json.dumps(images, ensure_ascii=False) if images else None

                # 提取评论时间（尝试多个键名）
                comment_time = None
                for key in ("create_at", "created_at", "time", "create_time"):
                    if key in comment and comment[key]:
                        comment_time = comment[key]
                        break
                if comment_time is None:
                    comment_time = int(datetime.now(timezone.utc).timestamp())

                cur.execute("""
                    INSERT INTO post_comments 
                    (link_id, comment_id, rank, username, user_id, avatar, 
                     text, up, has_image, images, comment_time, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    link_id,
                    comment.get("comment_id", 0),
                    rank,
                    comment.get("username", ""),
                    str(comment.get("user_id", "")),
                    comment.get("avatar", ""),
                    comment.get("text", ""),
                    comment.get("up", 0),
                    1 if comment.get("has_image", False) else 0,
                    images_str,
                    comment_time,
                    datetime.now(timezone.utc).isoformat()
                ))
                valid_count += 1

            # 更新 posts 表的评论计数
            cur.execute(
                "UPDATE posts SET top_comment_count = ? WHERE link_id = ?",
                (valid_count, link_id)
            )

            conn.commit()
            conn.close()
            logger.info(f"保存 {valid_count} 条评论到 link_id={link_id}")
        except Exception as e:
            logger.error(f"保存评论失败 link_id={link_id}: {e}")

    def get_top_comments(self, link_id: int) -> list[dict]:
        """获取帖子的前三评论（包含 comment_time）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT comment_id, rank, username, user_id, avatar, 
                       text, up, has_image, images, comment_time
                FROM post_comments
                WHERE link_id = ?
                ORDER BY rank
            """, (link_id,))
            rows = cur.fetchall()
            conn.close()

            results = []
            for row in rows:
                results.append({
                    "comment_id": row[0],
                    "rank": row[1],
                    "username": row[2],
                    "user_id": row[3],
                    "avatar": row[4],
                    "text": row[5],
                    "up": row[6],
                    "has_image": bool(row[7]),
                    "images": json.loads(row[8]) if row[8] else [],
                    "comment_time": row[9],
                })
            return results
        except Exception as e:
            logger.error(f"获取评论失败 link_id={link_id}: {e}")
            return []

    def delete_comments_by_link_id(self, link_id: int):
        """删除帖子的评论记录（用于帖子转移/删除时级联清理）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM post_comments WHERE link_id = ?", (link_id,))
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                logger.info(f"删除 link_id={link_id} 的 {deleted} 条评论记录")
        except Exception as e:
            logger.error(f"删除评论失败 link_id={link_id}: {e}")

    # ==================== 级联同步方法 ====================

    def sync_llm_analysis_by_link_id(self, link_id: int, new_daily_no: str = None,
                                      new_window_start: int = None) -> bool:
        """通过 link_id 级联同步 llm_analyses 的 daily_no/window_start"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            updates = []
            params = []
            if new_daily_no is not None:
                updates.append("daily_no = ?")
                params.append(str(new_daily_no))
            if new_window_start is not None:
                updates.append("window_start = ?")
                params.append(new_window_start)

            if not updates:
                conn.close()
                return True

            params.append(link_id)
            sql = f"UPDATE llm_analyses SET {', '.join(updates)} WHERE link_id = ?"
            cur.execute(sql, params)
            conn.commit()
            affected = cur.rowcount
            conn.close()

            if affected > 0:
                logger.info(f"级联同步成功: link_id={link_id} -> daily_no={new_daily_no}, window_start={new_window_start}")
            return True
        except Exception as e:
            logger.error(f"级联同步失败 link_id={link_id}: {e}")
            return False

    def sync_all_analyses_in_window(self, window_no: str) -> int:
        """同步整个窗口的所有分析记录"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute(
                "SELECT link_id, daily_no, window_start FROM posts WHERE date_str = ? ORDER BY daily_no",
                (window_no,)
            )
            posts_rows = cur.fetchall()

            synced = 0
            for link_id, daily_no, window_start in posts_rows:
                if not daily_no:
                    continue
                cur.execute(
                    "UPDATE llm_analyses SET daily_no = ?, window_start = ? WHERE link_id = ?",
                    (daily_no, window_start, link_id)
                )
                if cur.rowcount > 0:
                    synced += 1

            conn.commit()
            conn.close()
            if synced > 0:
                logger.info(f"窗口 {window_no} 级联同步完成: {synced} 条分析记录已修复")
            return synced
        except Exception as e:
            logger.error(f"批量级联同步失败 窗口={window_no}: {e}")
            return 0

    # ==================== 帖子基础操作 ====================

    def get_next_daily_no(self, window_no: str) -> str:
        """获取下一个 daily_no"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT daily_no FROM posts WHERE date_str = ? ORDER BY daily_no DESC LIMIT 1",
            (window_no,)
        )
        row = cur.fetchone()
        conn.close()

        if row and row[0]:
            _, seq = parse_daily_no(row[0])
            next_seq = seq + 1
        else:
            next_seq = 1

        return format_daily_no(window_no, next_seq)

    def get_existing_post(self, link_id: int) -> Optional[tuple]:
        """查询帖子是否已存在"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, content, image_urls, window_start, date_str, daily_no, source FROM posts WHERE link_id = ?",
            (link_id,)
        )
        row = cur.fetchone()
        conn.close()
        return row

    def _get_full_post(self, link_id: int) -> Optional[dict]:
        """获取帖子的完整信息"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT title, create_at, userid, username, avatar, topics, content, image_urls, source, top_comment_count "
            "FROM posts WHERE link_id = ?",
            (link_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        title, create_at, userid, username, avatar, topics, content, image_urls, old_source, top_comment_count = row
        return {
            "detail": {
                "title": title or "",
                "create_at": create_at or 0,
                "userid": userid or 0,
                "username": username or "",
                "avatar": avatar or "",
            },
            "content": content or "",
            "images": json.loads(image_urls) if image_urls else [],
            "topics": topics or "[]",
            "source": old_source or "feed",
            "top_comment_count": top_comment_count or 0,
        }

    def save_post(self, link_id: int, daily_no: Optional[str], window_start: Optional[int],
                  window_no: Optional[str], detail: dict, content_text: str, saved_images: list[str],
                  topics_str: str, source: str = "feed", top_comments: list[dict] = None):
        """保存帖子到数据库（包含评论）"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO posts
            (link_id, daily_no, window_start, date_str, title, create_at, userid, username, avatar,
             topics, content, image_urls, fetched_at, source, top_comment_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            link_id, daily_no, window_start, window_no,
            detail.get("title", ""), detail.get("create_at", 0),
            detail.get("userid", 0), detail.get("username", ""), detail.get("avatar", ""),
            topics_str, content_text, json.dumps(saved_images, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            source,
            len(top_comments) if top_comments else 0
        ))
        conn.commit()
        conn.close()

        if top_comments:
            self.save_top_comments(link_id, top_comments)

        if daily_no or window_start:
            self.sync_llm_analysis_by_link_id(link_id, daily_no, window_start)

    def get_posts_in_window(self, window_start: int) -> list[tuple]:
        """获取窗口内的所有帖子"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no FROM posts WHERE window_start = ? ORDER BY daily_no",
            (window_start,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def get_posts_by_window_no(self, window_no: str) -> list[tuple]:
        """根据窗口编号获取所有帖子"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no FROM posts WHERE date_str = ? ORDER BY daily_no",
            (window_no,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    # ========== 修复后的重新编号方法（避免 UNIQUE 冲突） ==========

    def renumber_window_posts(self, window_no: str) -> int:
        """重新编号指定窗口中的所有帖子（用于调整窗口后源窗口）"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        try:
            cur.execute(
                "SELECT link_id, daily_no, window_start FROM posts WHERE date_str = ? ORDER BY create_at, link_id",
                (window_no,)
            )
            rows = cur.fetchall()
            if not rows:
                conn.close()
                return 0

            window_start = rows[0][2]
            valid_link_ids = [row[0] for row in rows]

            # 1. 先将该窗口所有有效帖子的 llm_analyses 设为唯一临时值
            for link_id in valid_link_ids:
                temp_no = f"__temp_renum_{link_id}_{window_no}"
                cur.execute(
                    "UPDATE llm_analyses SET daily_no = ? WHERE link_id = ? AND window_start = ?",
                    (temp_no, link_id, window_start)
                )

            # 2. 重新分配新编号
            renumbered = 0
            for new_seq, (link_id, old_daily_no, ws) in enumerate(rows, start=1):
                new_daily_no = format_daily_no(window_no, new_seq)
                if old_daily_no != new_daily_no:
                    cur.execute(
                        "UPDATE posts SET daily_no = ? WHERE link_id = ? AND date_str = ?",
                        (new_daily_no, link_id, window_no)
                    )
                    cur.execute(
                        "UPDATE llm_analyses SET daily_no = ? WHERE link_id = ?",
                        (new_daily_no, link_id)
                    )
                    renumbered += 1
                    logger.info(f"重新编号: #{old_daily_no} -> #{new_daily_no}, link_id={link_id}")

            conn.commit()
            conn.close()
            if renumbered > 0:
                logger.info(f"窗口 {window_no} 重新编号完成: {renumbered} 个帖子")
            return renumbered
        except Exception as e:
            conn.rollback()
            conn.close()
            logger.error(f"renumber_window_posts 失败: {e}")
            return 0

    def swap_daily_no(self, window_no: str, seq1: int, seq2: int) -> tuple[bool, str]:
        """交换窗口内两个帖子的 daily_no（使用临时值避免唯一约束冲突）"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            # 关键修复：按 daily_no 排序，确保序号与编号一致
            cur.execute(
                "SELECT link_id, daily_no, window_start FROM posts WHERE date_str = ? ORDER BY daily_no",
                (window_no,)
            )
            rows = cur.fetchall()
            if not rows:
                return False, f"📭 窗口 {window_no} 内没有帖子"
            total = len(rows)
            if seq1 < 1 or seq1 > total:
                return False, f"❌ 序号 {seq1} 超出范围，窗口 {window_no} 共有 {total} 个帖子"
            if seq2 < 1 or seq2 > total:
                return False, f"❌ 序号 {seq2} 超出范围，窗口 {window_no} 共有 {total} 个帖子"
            if seq1 == seq2:
                return False, "❌ 两个序号相同，无需交换"

            link_id_1, old_no_1, ws1 = rows[seq1 - 1]
            link_id_2, old_no_2, ws2 = rows[seq2 - 1]

            # 使用临时值交换，避免唯一约束冲突
            temp_no = f"__tmp_swap_{link_id_1}_{int(datetime.now().timestamp())}"
            # 1) 将 link_id_1 改为临时值
            cur.execute("UPDATE posts SET daily_no = ? WHERE link_id = ?", (temp_no, link_id_1))
            cur.execute("UPDATE llm_analyses SET daily_no = ? WHERE link_id = ?", (temp_no, link_id_1))
            # 2) 将 link_id_2 改为 old_no_1
            cur.execute("UPDATE posts SET daily_no = ? WHERE link_id = ?", (old_no_1, link_id_2))
            cur.execute("UPDATE llm_analyses SET daily_no = ? WHERE link_id = ?", (old_no_1, link_id_2))
            # 3) 将 link_id_1 改为 old_no_2
            cur.execute("UPDATE posts SET daily_no = ? WHERE link_id = ?", (old_no_2, link_id_1))
            cur.execute("UPDATE llm_analyses SET daily_no = ? WHERE link_id = ?", (old_no_2, link_id_1))

            conn.commit()
            logger.info(f"✅ 交换完成: #{old_no_1} <-> #{old_no_2}, link_ids={link_id_1},{link_id_2}")
            return True, (
                f"✅ 交换成功！\n"
                f"📌 #{old_no_1} (ID:{link_id_1}) <-> #{old_no_2} (ID:{link_id_2})\n"
                f"📋 窗口 {window_no} 共 {total} 个帖子"
            )
        except Exception as e:
            conn.rollback()
            logger.error(f"交换 daily_no 失败: {e}")
            return False, f"❌ 交换失败: {str(e)}"
        finally:
            conn.close()

    def reset_daily_order(self, window_no: str) -> tuple[int, str]:
        """重置窗口内帖子的 daily_no 顺序（先设临时值再更新，避免唯一约束冲突）"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        try:
            cur.execute(
                "SELECT link_id, daily_no, create_at, window_start FROM posts WHERE date_str = ? ORDER BY create_at, link_id",
                (window_no,)
            )
            rows = cur.fetchall()
            if not rows:
                return 0, f"📭 窗口 {window_no} 内没有帖子"

            window_start = rows[0][3]
            valid_link_ids = [row[0] for row in rows]
            total = len(rows)

            # 1. 删除残留记录（不属于当前帖子的）
            if valid_link_ids:
                placeholders = ",".join(["?"] * len(valid_link_ids))
                cur.execute(f"""
                    DELETE FROM llm_analyses 
                    WHERE window_start = ? AND link_id NOT IN ({placeholders})
                """, (window_start, *valid_link_ids))
                deleted_count = cur.rowcount
                if deleted_count > 0:
                    logger.info(f"清理残留 AI 分析记录: 窗口 {window_no} 删除 {deleted_count} 条")

            # 2. 将当前窗口所有有效帖子的 daily_no 设为唯一临时值（防止更新时冲突）
            for link_id in valid_link_ids:
                temp_no = f"__temp_{link_id}_{window_no}"
                cur.execute(
                    "UPDATE llm_analyses SET daily_no = ? WHERE link_id = ? AND window_start = ?",
                    (temp_no, link_id, window_start)
                )

            # 3. 重新分配新编号
            renumbered = 0
            for new_seq, (link_id, old_daily_no, create_at, ws) in enumerate(rows, start=1):
                final_daily_no = format_daily_no(window_no, new_seq)
                cur.execute(
                    "UPDATE posts SET daily_no = ? WHERE link_id = ?",
                    (final_daily_no, link_id)
                )
                cur.execute(
                    "UPDATE llm_analyses SET daily_no = ? WHERE link_id = ?",
                    (final_daily_no, link_id)
                )
                if old_daily_no != final_daily_no:
                    renumbered += 1
                    logger.info(f"重新编号: #{old_daily_no} -> #{final_daily_no}, link_id={link_id}")

            conn.commit()
            if renumbered > 0:
                logger.info(f"窗口 {window_no} 重新编号完成: {renumbered}/{total} 个帖子")
                return renumbered, (
                    f"✅ 重置顺序成功！\n"
                    f"📋 窗口 {window_no} 共 {total} 个帖子\n"
                    f"🔄 重新编号 {renumbered} 个帖子\n"
                    f"📅 按发布时间排序后重新编号为 01 ~ {total:02d}"
                )
            else:
                return 0, (
                    f"✅ 顺序无需调整\n"
                    f"📋 窗口 {window_no} 共 {total} 个帖子\n"
                    f"📌 所有帖子编号已经是正确的顺序"
                )
        except Exception as e:
            conn.rollback()
            logger.error(f"重置 daily_no 顺序失败: {e}")
            return 0, f"❌ 重置失败: {str(e)}"
        finally:
            conn.close()

    # ==================== 图片相关 ====================

    def delete_image_analyses(self, link_id: int):
        """删除帖子的图片分析记录"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("DELETE FROM image_analyses WHERE link_id = ?", (link_id,))
        conn.commit()
        conn.close()

    async def download_image(self, url: str, filename: str) -> Optional[Path]:
        """下载单张图片"""
        if not url:
            return None
        try:
            save_path = self.image_dir / filename
            if save_path.exists():
                return save_path

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        save_path.write_bytes(data)
                        logger.info(f"图片下载成功: {filename}")
                        return save_path
                    else:
                        logger.warning(f"图片下载失败 HTTP {resp.status}: {url}")
                        return None
        except Exception as e:
            logger.error(f"图片下载异常: {e}, url={url}")
            return None

    async def download_images(self, link_id: int, image_urls: list[str]) -> list[str]:
        """批量下载帖子图片"""
        saved_images = []
        for i, img_url in enumerate(image_urls):
            ext = ".png"
            if ".jpg" in img_url or ".jpeg" in img_url:
                ext = ".jpg"
            elif ".webp" in img_url:
                ext = ".webp"
            filename = f"{link_id}_{i}{ext}"
            saved = await self.download_image(img_url, filename)
            if saved:
                saved_images.append(str(saved))
        return saved_images

    # ==================== 内容解析 ====================

    @staticmethod
    def parse_content(content_raw: str) -> tuple[str, list[str]]:
        """解析帖子内容，提取文本和图片 URL"""
        if not content_raw:
            return "", []
        try:
            blocks = json.loads(content_raw)
            if not isinstance(blocks, list):
                return str(content_raw), []

            text_parts, image_urls = [], []
            for block in blocks:
                block_type = block.get("type", "")
                if block_type in ("text", "html"):
                    text = block.get("text", "")
                    if text:
                        text_parts.append(text)
                elif block_type == "img":
                    url = block.get("url")
                    if url:
                        image_urls.append(url)
            return "\n".join(text_parts).strip(), image_urls
        except Exception:
            return str(content_raw), []

    @staticmethod
    def parse_topics(topics_list: list) -> str:
        """解析话题列表为 JSON 字符串"""
        topics_names = [t.get("name", "") for t in topics_list if isinstance(t, dict) and t.get("name")]
        return json.dumps(topics_names, ensure_ascii=False)

    # ==================== 子进程调用 ====================

    async def run_command(self, args: list[str]) -> dict:
        """运行外部命令"""
        cmd = [sys.executable, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.program_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return {
            "success": proc.returncode == 0,
            "stdout": stdout.decode("utf-8", errors="ignore"),
            "stderr": stderr.decode("utf-8", errors="ignore")
        }

    async def fetch_link_detail(self, link_id: int) -> Optional[dict]:
        """拉取单个帖子详情（包含 top_comments）"""
        script_path = self.program_path / "src" / "link.py"
        if not script_path.exists():
            script_path = self.program_path / "link.py"

        if not script_path.exists():
            logger.error(f"link.py 不存在，已查找: {self.program_path / 'src' / 'link.py'}")
            return None

        args = [str(script_path), "--link-id", str(link_id)]
        result = await self.run_command(args)
        if not result["success"]:
            logger.warning(f"拉取帖子详情失败 link_id={link_id}: {result['stderr'][:200]}")
            return None
        try:
            data = json.loads(result["stdout"])
            if "error" in data:
                logger.warning(f"link.py 返回错误 link_id={link_id}: {data['error']}")
                return None
            return data
        except Exception as e:
            logger.warning(f"解析帖子详情失败 link_id={link_id}: {e}")
            return None

    async def fetch_feed(self, topic_id: int, feed_limit: int = 10) -> list[dict]:
        """拉取社区 feed"""
        args = [
            "src/main.py",
            "--get-feed-ids",
            "--feed-topic-id", str(topic_id),
            "--feed-limit", str(feed_limit),
            "--feed-detail"
        ]
        result = await self.run_command(args)
        if not result["success"]:
            logger.error(f"拉取 feed 失败: {result['stderr']}")
            return []
        try:
            return json.loads(result["stdout"])
        except json.JSONDecodeError as e:
            logger.error(f"解析 feed JSON 失败: {e}\n输出: {result['stdout'][:500]}")
            return []

    @staticmethod
    def _extract_json_from_stdout(stdout: str) -> Optional[dict]:
        """从 stdout 中提取 JSON 对象"""
        if not stdout:
            return None

        start_pos = stdout.find('{')
        if start_pos == -1:
            return None

        json_str = stdout[start_pos:]

        for end_pos in range(len(json_str), 0, -1):
            try:
                result = json.loads(json_str[:end_pos])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

        return None

    async def fetch_at_messages(self, start_time: str = None, end_time: str = None,
                                 recent_hours: int = None) -> list[int]:
        """拉取 @ 消息，返回 link_id 列表"""
        args = ["src/at_fetcher.py"]

        if recent_hours is not None:
            args.extend(["--recent-hours", str(recent_hours)])
        elif start_time and end_time:
            args.extend(["--start-time", start_time, "--end-time", end_time])
        else:
            args.extend(["--recent-hours", "2"])

        result = await self.run_command(args)
        if not result["success"]:
            logger.error(f"拉取 @消息 失败: {result['stderr']}")
            return []

        stdout = result["stdout"]
        logger.debug(f"@消息原始输出前500字: {stdout[:500]}")

        try:
            data = self._extract_json_from_stdout(stdout)
            if data is None:
                logger.error(f"无法从 @消息输出中提取 JSON\n输出: {stdout[:500]}")
                return []

            link_ids = data.get("link_ids", [])
            count = data.get("count", 0)
            logger.info(f"@消息拉取成功: {count} 条, link_ids={link_ids}")
            return link_ids
        except Exception as e:
            logger.error(f"解析 @消息失败: {e}\n输出: {stdout[:500]}")
            return []

    # ==================== 帖子处理流程 ====================

    async def process_single_post(self, link_id: int, target_window_no: str = None,
                                   source: str = "feed", at_receive_time: int = None) -> bool:
        """处理单个帖子"""
        existing = self.get_existing_post(link_id)
        if existing:
            _, content, image_urls, old_window, old_date_str, old_daily_no, old_source = existing
            if content and image_urls:
                if source == "at" and target_window_no:
                    if old_date_str == target_window_no:
                        logger.info(f"link_id={link_id} 已完整拉取且在同一窗口({old_date_str})，跳过")
                        return False
                    logger.info(f"link_id={link_id} 已存在，@消息触发重新编号到窗口 {target_window_no} (原窗口={old_date_str})")
                    full_post = self._get_full_post(link_id)
                    if full_post:
                        window_start, window_end = get_window_by_no(target_window_no)
                        new_daily_no = self.get_next_daily_no(target_window_no)
                        self.save_post(
                            link_id, new_daily_no, window_start, target_window_no,
                            full_post["detail"], full_post["content"],
                            full_post["images"], full_post["topics"], source=source
                        )
                        logger.info(f"✅ 帖子重新编号: #{new_daily_no}, link_id={link_id}, 来源={source}")
                        return True
                    else:
                        logger.warning(f"link_id={link_id} 重新编号失败，无法读取完整数据")
                        return False
                else:
                    if old_date_str == target_window_no and old_source == source:
                        logger.info(f"link_id={link_id} 已完整拉取且在同一窗口同一来源，跳过")
                    else:
                        logger.info(f"link_id={link_id} 已完整拉取，窗口/来源不同，跳过")
                    return False

        detail = await self.fetch_link_detail(link_id)
        if not detail:
            logger.warning(f"拉取详情失败 link_id={link_id}，跳过")
            return False

        real_create_at = detail.get("create_at", 0)

        content_text, image_urls = self.parse_content(detail.get("content", ""))
        saved_images = await self.download_images(link_id, image_urls)
        topics_list = detail.get("topics", [])
        topics_str = self.parse_topics(topics_list)

        top_comments = detail.get("top_comments", [])

        if source == "at" and target_window_no:
            window_start, window_end = get_window_by_no(target_window_no)
            window_no = target_window_no
            logger.info(f"@消息 link_id={link_id} 强制归入窗口 {window_no}")
        else:
            window_start, window_end = get_window_for_timestamp(real_create_at)
            window_no = get_date_str_from_ts(window_end)
            logger.info(f"推荐流 link_id={link_id} 按发布时间归入窗口 {window_no}")

        daily_no = self.get_next_daily_no(window_no)
        self.save_post(
            link_id, daily_no, window_start, window_no, detail,
            content_text, saved_images, topics_str, source=source,
            top_comments=top_comments
        )
        logger.info(f"✅ 帖子入库: #{daily_no}, link_id={link_id}, 窗口={window_no}, 评论={len(top_comments)}条")
        return True

    async def process_posts(self, link_ids: list[int], source: str = "feed",
                           target_window_no: str = None, at_receive_time: int = None) -> int:
        """批量处理帖子列表"""
        if not link_ids:
            return 0

        if target_window_no:
            window_start, window_end = get_window_by_no(target_window_no)
            log_window_no = target_window_no
        else:
            window_start, window_end = get_current_window()
            log_window_no = get_current_window_no()

        logger.info(f"处理帖子列表: 窗口={log_window_no}, 共 {len(link_ids)} 个, 来源={source}")

        processed_count = 0
        for idx, link_id in enumerate(link_ids):
            success = await self.process_single_post(
                link_id, target_window_no=target_window_no,
                source=source, at_receive_time=at_receive_time
            )
            if success:
                processed_count += 1

            if idx < len(link_ids) - 1:
                wait_sec = self.content_fetch_interval_seconds
                logger.info(f"等待 {wait_sec} 秒后处理下一个...")
                await asyncio.sleep(wait_sec)

        logger.info(f"本次处理 {processed_count}/{len(link_ids)} 个帖子 (来源={source}, 窗口={log_window_no})")
        return processed_count