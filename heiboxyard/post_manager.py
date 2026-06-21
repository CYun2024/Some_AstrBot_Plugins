"""
小黑盒帖子管理模块
负责：数据库操作、图片下载、内容解析、帖子入库流程
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
    get_today_window, ts_to_bj_str, get_date_str_from_ts,
    get_date_str_from_window, format_daily_no, parse_daily_no,
    get_window_for_timestamp
)


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

    def _ensure_db(self):
        """确保数据库表存在（处理数据库文件被外部删除的情况）"""
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
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

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
                    source TEXT DEFAULT 'feed'
                )
            """)
            cur.execute("CREATE INDEX idx_posts_window ON posts(window_start)")
            cur.execute("CREATE INDEX idx_posts_window_no ON posts(window_start, daily_no)")
            cur.execute("CREATE INDEX idx_posts_date ON posts(date_str)")
            logger.info("帖子表初始化完成")
        else:
            self._ensure_table_schema(conn)
            # 确保索引存在
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

        conn.commit()
        conn.close()

    def get_next_daily_no(self, date_str: str) -> str:
        """获取下一个 daily_no（新格式：YYYYMMDD-NN）"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # 查找该日期下最大的编号
        cur.execute(
            "SELECT daily_no FROM posts WHERE date_str = ? ORDER BY daily_no DESC LIMIT 1",
            (date_str,)
        )
        row = cur.fetchone()
        conn.close()

        if row and row[0]:
            _, seq = parse_daily_no(row[0])
            next_seq = seq + 1
        else:
            next_seq = 1

        return format_daily_no(date_str, next_seq)

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
        """获取帖子的完整信息（用于重新编号）"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT title, create_at, userid, username, avatar, topics, content, image_urls, source "
            "FROM posts WHERE link_id = ?",
            (link_id,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        title, create_at, userid, username, avatar, topics, content, image_urls, old_source = row
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
        }

    def save_post(self, link_id: int, daily_no: Optional[str], window_start: Optional[int],
                  date_str: Optional[str], detail: dict, content_text: str, saved_images: list[str],
                  topics_str: str, source: str = "feed"):
        """保存帖子到数据库"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO posts
            (link_id, daily_no, window_start, date_str, title, create_at, userid, username, avatar,
             topics, content, image_urls, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            link_id, daily_no, window_start, date_str,
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

    def get_posts_by_date(self, date_str: str) -> list[tuple]:
        """获取指定日期的所有帖子"""
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no FROM posts WHERE date_str = ? ORDER BY daily_no",
            (date_str,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    def delete_image_analyses(self, link_id: int):
        """删除帖子的图片分析记录（用于重置）"""
        self._ensure_db()
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

        start_pos = stdout.find('{')
        if start_pos == -1:
            logger.debug(f"未在输出中找到 JSON 对象起始位置 {{")
            return None

        json_str = stdout[start_pos:]

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
                                   source: str = "feed", at_receive_time: int = None) -> bool:
        """处理单个帖子：拉取详情、下载图片、入库"""
        existing = self.get_existing_post(link_id)
        if existing:
            _, content, image_urls, old_window, old_date, old_daily_no, old_source = existing
            # 内容已完整拉取过
            if content and image_urls:
                # @消息来源：检查是否需要重新编号到当前窗口
                if source == "at":
                    # 计算当前窗口的 date_str（daily_no 前缀）
                    current_date_str = get_date_str_from_window(window_start)
                    # 用 date_str 判断是否已在当前窗口（兼容旧数据 window_start 可能不准确的情况）
                    if old_date == current_date_str:
                        logger.info(f"link_id={link_id} 已完整拉取且在同一窗口(date_str={old_date})，跳过")
                        return False
                    # 重新分配到当前窗口
                    logger.info(f"link_id={link_id} 已存在，@消息触发重新编号到当前窗口 (原date_str={old_date}, 新date_str={current_date_str})")
                    full_post = self._get_full_post(link_id)
                    if full_post:
                        new_daily_no = self.get_next_daily_no(current_date_str)
                        self.save_post(
                            link_id, new_daily_no, window_start, current_date_str,
                            full_post["detail"], full_post["content"],
                            full_post["images"], full_post["topics"], source=source
                        )
                        logger.info(f"✅ 帖子重新编号: #{new_daily_no}, link_id={link_id}, 来源={source}")
                        return True
                    else:
                        logger.warning(f"link_id={link_id} 重新编号失败，无法读取完整数据")
                        return False
                else:
                    # feed 来源：跳过
                    if old_window == window_start and old_source == source:
                        logger.info(f"link_id={link_id} 已完整拉取且在同一窗口同一来源，跳过")
                    else:
                        logger.info(f"link_id={link_id} 已完整拉取，窗口/来源不同，跳过")
                    return False

        detail = await self.fetch_link_detail(link_id)
        if not detail:
            logger.warning(f"拉取详情失败 link_id={link_id}，跳过")
            return False

        real_create_at = detail.get("create_at", 0)
        real_create_str = detail.get("create_at_str", "")

        # 解析内容和下载图片
        content_raw = detail.get("content", "")
        content_text, image_urls = self.parse_content(content_raw)
        saved_images = await self.download_images(link_id, image_urls)
        topics_list = detail.get("topics", [])
        topics_str = self.parse_topics(topics_list)

        # 确定归属：根据帖子自身时间计算窗口，而不是拉取时的当前窗口
        if source == "at" and at_receive_time:
            # @消息：按收到时间计算窗口
            post_window_start, post_window_end = get_window_for_timestamp(at_receive_time)
            # daily_no 的日期前缀应对应窗口结束日
            date_str = get_date_str_from_ts(post_window_end)
            window_start_for_post = post_window_start
            in_window = True
            logger.info(f"@消息 link_id={link_id} 按收到时间归入 {date_str}, 窗口={ts_to_bj_str(post_window_start)}~{ts_to_bj_str(post_window_end)}")
        else:
            # 推荐流：按帖子实际发布时间计算窗口
            post_window_start, post_window_end = get_window_for_timestamp(real_create_at)
            # daily_no 的日期前缀应对应窗口结束日
            date_str = get_date_str_from_ts(post_window_end)
            window_start_for_post = post_window_start
            in_window = True
            logger.info(f"推荐流 link_id={link_id} 按发布时间归入 {date_str}, 窗口={ts_to_bj_str(post_window_start)}~{ts_to_bj_str(post_window_end)}")

        # 获取编号
        if in_window:
            daily_no = self.get_next_daily_no(date_str)
            self.save_post(link_id, daily_no, window_start_for_post, date_str, detail,
                          content_text, saved_images, topics_str, source=source)
            logger.info(f"✅ 帖子入库: daily_no=#{daily_no}, link_id={link_id}, "
                       f"归属日期={date_str}, 作者={detail.get('username', '')}, 来源={source}")
        else:
            # 归档：不给编号
            self.save_post(link_id, None, None, date_str, detail, content_text,
                          saved_images, topics_str, source=source)
            logger.info(f"📌 帖子归档: link_id={link_id}, 归属日期={date_str}, 来源={source}")

        return True

    async def process_posts(self, link_ids: list[int], source: str = "feed",
                           at_receive_time: int = None) -> int:
        """
        批量处理帖子列表
        """
        if not link_ids:
            return 0

        window_start, window_end = get_today_window()
        today_date = get_date_str_from_window(window_start)
        logger.info(f"处理帖子列表: 时间窗口 {ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)} (北京时间), "
                   f"今日日期={today_date}, 共 {len(link_ids)} 个帖子, 来源={source}")

        processed_count = 0
        for idx, link_id in enumerate(link_ids):
            success = await self.process_single_post(
                link_id, window_start, window_end,
                source=source, at_receive_time=at_receive_time
            )
            if success:
                processed_count += 1

            if idx < len(link_ids) - 1:
                wait_sec = self.content_fetch_interval_seconds
                logger.info(f"等待 {wait_sec} 秒后处理下一个...")
                await asyncio.sleep(wait_sec)

        logger.info(f"本次共处理 {processed_count}/{len(link_ids)} 个帖子 (来源={source})")
        return processed_count