"""
用户历史帖子评价记忆数据库
负责：存储每个帖主的历史帖子及AI评价，供后续分析时构建记忆
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger


class UserMemoryDB:
    """用户历史记忆数据库"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化记忆表"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # 用户历史帖子记忆表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid INTEGER NOT NULL,
                username TEXT,
                link_id INTEGER NOT NULL,
                window_start INTEGER,
                title TEXT,
                content_summary TEXT,
                ai_comment TEXT,
                score REAL,
                sentiment TEXT,
                tags TEXT,
                created_at TEXT,
                UNIQUE(userid, link_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_user ON user_memories(userid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_link ON user_memories(link_id)")

        # 用户画像汇总表（用于快速查询）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                userid INTEGER PRIMARY KEY,
                username TEXT,
                post_count INTEGER DEFAULT 0,
                avg_score REAL,
                common_tags TEXT,
                common_sentiment TEXT,
                last_post_at TEXT,
                updated_at TEXT
            )
        """)

        conn.commit()
        conn.close()
        logger.info("用户记忆表初始化完成")

    def save_memory(self, userid: int, username: str, link_id: int,
                    window_start: int, title: str, content_summary: str,
                    ai_comment: str, score: float, sentiment: str, tags: list):
        """保存单条帖子记忆"""
        if not userid:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO user_memories
                (userid, username, link_id, window_start, title, content_summary,
                 ai_comment, score, sentiment, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                userid, username or "", link_id, window_start, title or "",
                content_summary or "", ai_comment or "", score,
                sentiment or "", json.dumps(tags, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat()
            ))
            conn.commit()
            conn.close()
            self._update_user_profile(userid, username)
        except Exception as e:
            logger.error(f"保存用户记忆失败 userid={userid}: {e}")

    def _update_user_profile(self, userid: int, username: str):
        """更新用户画像汇总"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 统计用户历史数据
            cur.execute("""
                SELECT COUNT(*)
                FROM user_memories WHERE userid = ?
            """, (userid,))
            row = cur.fetchone()
            post_count = row[0] or 0
            avg_score = 0  # 新版评论模式不再评分，固定为0

            # 标签统计（当前版本已禁用 LLM 标签生成，保留空列表兼容）
            common_tags = []

            # 逐行读取情感
            cur.execute("""
                SELECT sentiment FROM user_memories
                WHERE userid = ? AND sentiment IS NOT NULL AND sentiment != ''
            """, (userid,))
            sentiments = [row[0] for row in cur.fetchall()]
            common_sentiment = Counter(sentiments).most_common(1)
            common_sentiment = common_sentiment[0][0] if common_sentiment else "neutral"

            cur.execute("""
                INSERT OR REPLACE INTO user_profiles
                (userid, username, post_count, avg_score, common_tags, common_sentiment, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                userid, username or "", post_count, avg_score,
                json.dumps(common_tags, ensure_ascii=False), common_sentiment,
                datetime.now(timezone.utc).isoformat()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"更新用户画像失败 userid={userid}: {e}")

    def get_user_history(self, userid: int, limit: int = 5) -> list[dict]:
        """获取用户最近的历史帖子评价"""
        if not userid:
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT link_id, title, content_summary, ai_comment, score, sentiment, tags, created_at
                FROM user_memories WHERE userid = ?
                ORDER BY created_at DESC LIMIT ?
            """, (userid, limit))
            rows = cur.fetchall()
            conn.close()

            results = []
            for row in rows:
                results.append({
                    "link_id": row[0],
                    "title": row[1],
                    "content_summary": row[2],
                    "ai_comment": row[3],
                    "score": row[4] or 0,
                    "sentiment": row[5] or "neutral",
                    "tags": json.loads(row[6]) if row[6] else [],
                    "created_at": row[7],
                })
            return results
        except Exception as e:
            logger.error(f"获取用户历史失败 userid={userid}: {e}")
            return []

    def get_user_profile(self, userid: int) -> Optional[dict]:
        """获取用户画像"""
        if not userid:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT username, post_count, avg_score, common_tags, common_sentiment
                FROM user_profiles WHERE userid = ?
            """, (userid,))
            row = cur.fetchone()
            conn.close()

            if row:
                return {
                    "username": row[0],
                    "post_count": row[1],
                    "avg_score": row[2],
                    "common_tags": json.loads(row[3]) if row[3] else [],
                    "common_sentiment": row[4],
                }
            return None
        except Exception as e:
            logger.error(f"获取用户画像失败 userid={userid}: {e}")
            return None

    def build_memory_context(self, userid: int, username: str) -> str:
        """构建用户记忆上下文文本，用于 prompt"""
        if not userid:
            return ""

        profile = self.get_user_profile(userid)
        history = self.get_user_history(userid, limit=3)

        lines = []
        if profile:
            lines.append(f"【{username} 的历史画像】")
            lines.append(f"- 历史发帖数: {profile['post_count']}")
            # lines.append(f"- 常见标签: {', '.join(profile['common_tags']) or '无'}")  # 已禁用
            lines.append(f"- 主要情感倾向: {profile['common_sentiment']}")

        if history:
            lines.append(f"\n【{username} 的近期帖子及评价】")
            for i, h in enumerate(history, 1):
                lines.append(f"{i}. 《{h['title']}》")
                lines.append(f"   AI评价: {h['ai_comment']}")
                lines.append(f"   评分: {h['score']}/10")

        return "\n".join(lines) if lines else ""