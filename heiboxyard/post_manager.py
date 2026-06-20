"""
小黑盒帖子管理模块
负责：数据库操作、图片下载、内容解析、帖子入库流程
"""
import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from astrbot.api import logger

from .utils import get_today_window, ts_to_bj_str


class PostManager:
    """帖子管理器：处理数据库、图片下载、帖子入库"""

    def __init__(self, db_path: Path, image_dir: Path, program_path: Path,
                 content_fetch_interval_seconds: int = 30):
        self.db_path = db_path
        self.image_dir = image_dir
        self.program_path = program_path
        self.content_fetch_interval_seconds = content_fetch_interval_seconds
        self._init_db()

    # ==================== 数据库 ====================

    def _ensure_table_schema(self, conn):
        """确保 posts 表包含所有必要字段"""
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(posts)")
        existing_cols = {row[1] for row in cur.fetchall()}

        migrations = []
        if "daily_no" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN daily_no INTEGER")
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

        for sql in migrations:
            cur.execute(sql)
            logger.info(f"数据库迁移: {sql}")

        if migrations:
            conn.commit()
            logger.info("数据库迁移完成")

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
        table_exists = cur.fetchone() is not None

        if not table_exists:
            cur.execute("""
                CREATE TABLE posts (
                    link_id INTEGER PRIMARY KEY,
                    daily_no INTEGER,
                    window_start INTEGER,
                    title TEXT,
                    create_at INTEGER,
                    userid INTEGER,
                    username TEXT,
                    avatar TEXT,
                    topics TEXT,
                    content TEXT,
                    image_urls TEXT,
                    fetched_at TEXT,
                    source TEXT DEFAULT 'feed'
                )
            """)
            cur.execute("CREATE INDEX idx_posts_window ON posts(window_start)")
            cur.execute("CREATE INDEX idx_posts_window_no ON posts(window_start, daily_no)")
            logger.info("帖子表初始化完成")
        else:
            self._ensure_table_schema(conn)
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_posts_window'")
            if not cur.fetchone():
                cur.execute("CREATE INDEX idx_posts_window ON posts(window_start)")
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_posts_window_no'")
            if not cur.fetchone():
                cur.execute("CREATE INDEX idx_posts_window_no ON posts(window_start, daily_no)")

        conn.commit()
        conn.close()

    def get_next_daily_no(self, window_start: int) -> int:
        """获取下一个 daily_no"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT MAX(daily_no) FROM posts WHERE window_start = ?", (window_start,))
        result = cur.fetchone()[0]
        conn.close()
        return (result or 0) + 1

    def get_existing_post(self, link_id: int) -> Optional[tuple]:
        """查询帖子是否已存在"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, content, image_urls, window_start FROM posts WHERE link_id = ?",
            (link_id,)
        )
        row = cur.fetchone()
        conn.close()
        return row

    def save_post(self, link_id: int, daily_no: Optional[int], window_start: Optional[int],
                  detail: dict, content_text: str, saved_images: list[str],
                  topics_str: str, source: str = "feed"):
        """保存帖子到数据库"""
        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO posts
            (link_id, daily_no, window_start, title, create_at, userid, username, avatar,
             topics, content, image_urls, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            link_id, daily_no, window_start,
            detail.get("title", ""), detail.get("create_at", 0),
            detail.get("userid", 0), detail.get("username", ""), detail.get("avatar", ""),
            topics_str, content_text, json.dumps(saved_images, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            source
        ))
        conn.commit()
        conn.close()

    def get_posts_in_window(self, window_start: int) -> list[tuple]:
        """获取窗口内的所有帖子"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no FROM posts WHERE window_start = ? ORDER BY daily_no",
            (window_start,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def delete_image_analyses(self, link_id: int):
        """删除帖子的图片分析记录（用于重置）"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("DELETE FROM image_analyses WHERE link_id = ?", (link_id,))
        conn.commit()
        conn.close()

    # ==================== 图片下载 ====================

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
        """拉取单个帖子详情"""
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
        """从 stdout 中提取 JSON 对象（以 { 开头），忽略 [INFO] 等日志前缀"""
        if not stdout:
            return None

        # 只找 { 开头的 JSON 对象，避免 [INFO] 的 [ 被误判
        start_pos = stdout.find('{')
        if start_pos == -1:
            logger.debug(f"未在输出中找到 JSON 对象起始位置 {{")
            return None

        json_str = stdout[start_pos:]

        # 从完整字符串开始尝试解析，逐步截断尾部找合法 JSON
        for end_pos in range(len(json_str), 0, -1):
            try:
                result = json.loads(json_str[:end_pos])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

        logger.debug(f"无法从输出中提取合法 JSON 对象")
        return None

    async def fetch_at_messages(self, start_time: str = None, end_time: str = None,
                                 recent_hours: int = None) -> list[int]:
        """
        拉取 @ 消息，返回 link_id 列表
        Args:
            start_time: 开始时间，格式 "YYYY-MM-DD HH:MM:SS"
            end_time: 结束时间，格式 "YYYY-MM-DD HH:MM:SS"
            recent_hours: 最近 N 小时（与 start_time/end_time 互斥）
        """
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

    async def process_single_post(self, link_id: int, window_start: int, window_end: int,
                                   source: str = "feed") -> bool:
        """处理单个帖子：拉取详情、下载图片、入库"""
        existing = self.get_existing_post(link_id)
        if existing:
            _, content, image_urls, old_window = existing
            # 内容已完整拉取过，直接跳过，不再重新拉取
            if content and image_urls:
                if old_window == window_start:
                    logger.info(f"link_id={link_id} 已完整拉取且在同一窗口，跳过")
                else:
                    logger.info(f"link_id={link_id} 已完整拉取，窗口不同 ({old_window} vs {window_start})，跳过")
                return False

        detail = await self.fetch_link_detail(link_id)
        if not detail:
            logger.warning(f"拉取详情失败 link_id={link_id}，跳过")
            return False

        real_create_at = detail.get("create_at", 0)
        real_create_str = detail.get("create_at_str", "")
        in_window = window_start <= real_create_at < window_end

        content_raw = detail.get("content", "")
        content_text, image_urls = self.parse_content(content_raw)
        saved_images = await self.download_images(link_id, image_urls)

        topics_list = detail.get("topics", [])
        topics_str = self.parse_topics(topics_list)

        if in_window:
            daily_no = self.get_next_daily_no(window_start)
            self.save_post(link_id, daily_no, window_start, detail, content_text,
                           saved_images, topics_str, source=source)
            logger.info(f"✅ 帖子入库(今日): daily_no=#{daily_no}, link_id={link_id}, "
                       f"发布时间={real_create_str}, 作者={detail.get('username', '')}, 来源={source}")
        else:
            self.save_post(link_id, None, None, detail, content_text,
                           saved_images, topics_str, source=source)
            logger.info(f"📌 帖子入库(归档): link_id={link_id}, 发布时间={real_create_str}, "
                       f"不在窗口 {ts_to_bj_str(window_start)}~{ts_to_bj_str(window_end)}, 来源={source}")

        return True

    async def process_posts(self, link_ids: list[int], source: str = "feed") -> int:
        """批量处理帖子列表"""
        if not link_ids:
            return 0

        window_start, window_end = get_today_window()
        logger.info(f"处理帖子列表: 时间窗口 {ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)} (北京时间), "
                   f"共 {len(link_ids)} 个帖子, 来源={source}")

        processed_count = 0
        for idx, link_id in enumerate(link_ids):
            success = await self.process_single_post(link_id, window_start, window_end, source=source)
            if success:
                processed_count += 1

            if idx < len(link_ids) - 1:
                wait_sec = self.content_fetch_interval_seconds
                logger.info(f"等待 {wait_sec} 秒后处理下一个...")
                await asyncio.sleep(wait_sec)

        logger.info(f"本次共处理 {processed_count}/{len(link_ids)} 个帖子 (来源={source})")
        return processed_count