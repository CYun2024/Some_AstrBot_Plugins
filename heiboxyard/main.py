import asyncio
import base64
import json
import sqlite3
import sys
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig

from .llm_analyzer import LLMPostAnalyzer
from .image_analyzer import ImagePostAnalyzer
from .memory_db import UserMemoryDB
from .report_generator import EveningReportGenerator

DEFAULT_PROGRAM_PATH = "heibox-comment-bot-master"


def clean_html_tags(text: str) -> str:
    if not text:
        return text
    cleaned = re.sub(r'<[^>]+>', ' ', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def get_today_window() -> tuple[int, int]:
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    today_22 = now_bj.replace(hour=22, minute=0, second=0, microsecond=0)
    if now_bj.hour >= 22:
        window_start = today_22
        window_end = today_22 + timedelta(days=1)
    else:
        window_start = today_22 - timedelta(days=1)
        window_end = today_22
    return (
        int(window_start.astimezone(timezone.utc).timestamp()),
        int(window_end.astimezone(timezone.utc).timestamp())
    )


def get_analysis_window() -> tuple[int, int]:
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    today_22 = now_bj.replace(hour=22, minute=0, second=0, microsecond=0)
    window_start = today_22 - timedelta(days=1)
    window_end = today_22
    return (
        int(window_start.astimezone(timezone.utc).timestamp()),
        int(window_end.astimezone(timezone.utc).timestamp())
    )


def ts_to_bj_str(timestamp: int) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@register("heiboxyard", "YourName", "小黑盒帖子自动拉取插件", "1.0.0", "https://github.com/your/repo")
class HeiboxYard(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context

        if config is None:
            config = {}
        self.config = config

        self.interval_hours = config.get("interval_hours", 2)
        self.feed_topic_ids = config.get("feed_topic_ids", [486620])
        self.topic_fetch_interval_minutes = config.get("topic_fetch_interval_minutes", 5)
        self.content_fetch_interval_seconds = config.get("content_fetch_interval_seconds", 30)

        if isinstance(self.feed_topic_ids, str):
            try:
                self.feed_topic_ids = [int(x.strip()) for x in self.feed_topic_ids.split(",") if x.strip()]
            except ValueError:
                self.feed_topic_ids = [486620]
        elif not isinstance(self.feed_topic_ids, list):
            self.feed_topic_ids = [486620]

        self.llm_analysis_enabled = config.get("llm_analysis_enabled", True)
        self.llm_analysis_time = config.get("llm_analysis_time", "22:30")
        self.llm_provider_id = config.get("llm_provider_id", "")
        self.vision_provider_id = config.get("vision_provider_id", "")
        self.llm_analysis_batch_size = config.get("llm_analysis_batch_size", 8)
        self.llm_analysis_batch_interval = config.get("llm_analysis_batch_interval_seconds", 5)

        # 晚报配置
        self.evening_report_enabled = config.get("evening_report_enabled", True)
        self.evening_report_auto_send = config.get("evening_report_auto_send", False)
        self.evening_report_time = config.get("evening_report_time", "23:00")
        self.evening_report_format = config.get("evening_report_format", "image")
        self.evening_report_target_group = config.get("evening_report_target_group", "")

        logger.info(f"配置加载: interval_hours={self.interval_hours}, feed_topic_ids={self.feed_topic_ids}")
        logger.info(f"LLM分析: enabled={self.llm_analysis_enabled}, time={self.llm_analysis_time}")
        logger.info(f"晚报: enabled={self.evening_report_enabled}, auto_send={self.evening_report_auto_send}, time={self.evening_report_time}, format={self.evening_report_format}")

        raw_path = self.plugin_config.get("program_path", DEFAULT_PROGRAM_PATH) if hasattr(self, 'plugin_config') else DEFAULT_PROGRAM_PATH

        plugin_dir = Path(__file__).parent.resolve()
        program_path = plugin_dir / raw_path
        if not program_path.exists():
            program_path = Path(raw_path).resolve()

        self.program_path = program_path
        self.plugin_dir = plugin_dir

        if not self.program_path.exists():
            logger.error(f"小黑盒程序路径不存在: {self.program_path}")
            logger.info(f"插件目录: {plugin_dir}")

        data_dir = plugin_dir / "data"
        data_dir.mkdir(exist_ok=True)
        self.db_path = data_dir / "posts.db"
        self.image_dir = data_dir / "images"
        self.image_dir.mkdir(exist_ok=True)
        self._init_db()

        self.image_analyzer = ImagePostAnalyzer(
            context=self.context,
            db_path=self.db_path,
            vision_provider_id=self.vision_provider_id if self.vision_provider_id else None
        )

        self.memory_db = UserMemoryDB(self.db_path)

        self.llm_analyzer = LLMPostAnalyzer(
            context=self.context,
            db_path=self.db_path,
            chat_provider_id=self.llm_provider_id if self.llm_provider_id else None,
            memory_db=self.memory_db,
            image_analyzer=self.image_analyzer
        )

        template_dir = str(plugin_dir / "templates")
        self.report_generator = EveningReportGenerator(
            template_dir=template_dir,
            data_dir=str(data_dir)
        )

        self._fetch_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._bg_task = asyncio.create_task(self._background_loop())
        self._nightly_task = asyncio.create_task(self._nightly_fetch_loop())
        self._llm_analysis_task = asyncio.create_task(self._llm_analysis_loop())
        self._evening_report_task = asyncio.create_task(self._evening_report_loop())

    # ==================== 数据库 ====================

    def _ensure_table_schema(self, conn):
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

        for sql in migrations:
            cur.execute(sql)
            logger.info(f"数据库迁移: {sql}")

        if migrations:
            conn.commit()
            logger.info("数据库迁移完成")

    def _init_db(self):
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
                    fetched_at TEXT
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

    def _get_next_daily_no(self, conn, window_start: int) -> int:
        cur = conn.cursor()
        cur.execute("SELECT MAX(daily_no) FROM posts WHERE window_start = ?", (window_start,))
        result = cur.fetchone()[0]
        return (result or 0) + 1

    # ==================== 图片下载 ====================

    async def _download_image(self, url: str, filename: str) -> Optional[Path]:
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

    # ==================== 子进程 ====================

    async def _run_command(self, args: list[str]) -> dict:
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

    # ==================== Feed拉取 ====================

    async def _fetch_feed(self, topic_id: int, feed_limit: int = 10) -> list[dict]:
        args = [
            "src/main.py",
            "--get-feed-ids",
            "--feed-topic-id", str(topic_id),
            "--feed-limit", str(feed_limit),
            "--feed-detail"
        ]
        result = await self._run_command(args)
        if not result["success"]:
            logger.error(f"拉取 feed 失败: {result['stderr']}")
            return []
        try:
            return json.loads(result["stdout"])
        except json.JSONDecodeError as e:
            logger.error(f"解析 feed JSON 失败: {e}\n输出: {result['stdout'][:500]}")
            return []

    async def _fetch_link_detail(self, link_id: int) -> Optional[dict]:
        script_path = self.program_path / "src" / "link.py"
        if not script_path.exists():
            script_path = self.program_path / "link.py"

        if not script_path.exists():
            logger.error(f"link.py 不存在，已查找: {self.program_path / 'src' / 'link.py'}")
            return None

        args = [str(script_path), "--link-id", str(link_id)]
        result = await self._run_command(args)
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

    def _parse_content(self, content_raw: str) -> tuple[str, list[str]]:
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

    # ==================== 帖子处理 ====================

    async def _process_new_posts(self, feed_items: list[dict]):
        if not feed_items:
            logger.info("feed 为空，没有新帖子")
            return

        window_start, window_end = get_today_window()
        logger.info(f"今日时间窗口: {ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)} (北京时间)")

        candidate_link_ids = []
        for item in feed_items:
            modify_at = item.get("create_at", 0)
            link_id = item["link_id"]

            if window_start <= modify_at < window_end:
                candidate_link_ids.append(link_id)
                logger.info(f"Feed 中 link_id={link_id} modify_at={ts_to_bj_str(modify_at)} 在窗口内，将拉取详情")
            else:
                logger.info(f"Feed 中 link_id={link_id} modify_at={ts_to_bj_str(modify_at)} 不在窗口内，跳过")

        if not candidate_link_ids:
            logger.info("没有候选帖子需要拉取详情")
            return

        processed_count = 0
        for idx, link_id in enumerate(candidate_link_ids):
            conn = sqlite3.connect(self.db_path)
            self._ensure_table_schema(conn)
            cur = conn.cursor()
            cur.execute(
                "SELECT link_id, content, image_urls, window_start FROM posts WHERE link_id = ?",
                (link_id,)
            )
            existing = cur.fetchone()
            conn.close()

            if existing:
                _, content, image_urls, old_window = existing
                if content and image_urls and old_window == window_start:
                    logger.info(f"link_id={link_id} 已完整拉取且在同一窗口，跳过")
                    continue
                elif content and image_urls:
                    logger.info(f"link_id={link_id} 已拉取但窗口不同 ({old_window} vs {window_start})，重新处理")

            detail = await self._fetch_link_detail(link_id)
            if not detail:
                logger.warning(f"拉取详情失败 link_id={link_id}，跳过")
                continue

            real_create_at = detail.get("create_at", 0)
            real_create_str = detail.get("create_at_str", "")

            in_window = window_start <= real_create_at < window_end

            content_raw = detail.get("content", "")
            content_text, image_urls = self._parse_content(content_raw)

            saved_images = []
            for i, img_url in enumerate(image_urls):
                ext = ".png"
                if ".jpg" in img_url or ".jpeg" in img_url:
                    ext = ".jpg"
                elif ".webp" in img_url:
                    ext = ".webp"
                filename = f"{link_id}_{i}{ext}"
                saved = await self._download_image(img_url, filename)
                if saved:
                    saved_images.append(str(saved))

            if saved_images:
                logger.info(f"开始分析 link_id={link_id} 的 {len(saved_images)} 张图片")
                await self.image_analyzer.analyze_images(link_id, saved_images)

            topics_list = detail.get("topics", [])
            topics_names = [t.get("name", "") for t in topics_list if isinstance(t, dict) and t.get("name")]
            topics_str = json.dumps(topics_names, ensure_ascii=False)

            conn = sqlite3.connect(self.db_path)
            self._ensure_table_schema(conn)
            cur = conn.cursor()

            if in_window:
                daily_no = self._get_next_daily_no(conn, window_start)
                cur.execute("""
                    INSERT OR REPLACE INTO posts 
                    (link_id, daily_no, window_start, title, create_at, userid, username, avatar, topics, content, image_urls, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    link_id, daily_no, window_start,
                    detail.get("title", ""), real_create_at,
                    detail.get("userid", 0), detail.get("username", ""), detail.get("avatar", ""),
                    topics_str, content_text, json.dumps(saved_images, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat()
                ))
                logger.info(f"✅ 帖子入库(今日): daily_no=#{daily_no}, link_id={link_id}, 发布时间={real_create_str}, 作者={detail.get('username', '')}")
            else:
                cur.execute("""
                    INSERT OR REPLACE INTO posts 
                    (link_id, daily_no, window_start, title, create_at, userid, username, avatar, topics, content, image_urls, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    link_id, None, None,
                    detail.get("title", ""), real_create_at,
                    detail.get("userid", 0), detail.get("username", ""), detail.get("avatar", ""),
                    topics_str, content_text, json.dumps(saved_images, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat()
                ))
                logger.info(f"📌 帖子入库(归档): link_id={link_id}, 发布时间={real_create_str}, 不在窗口 {ts_to_bj_str(window_start)}~{ts_to_bj_str(window_end)}")

            conn.commit()
            conn.close()
            processed_count += 1

            if idx < len(candidate_link_ids) - 1:
                wait_sec = self.content_fetch_interval_seconds
                logger.info(f"等待 {wait_sec} 秒后拉取下一个...")
                await asyncio.sleep(wait_sec)

        logger.info(f"本次共处理 {processed_count} 个帖子")

    async def _fetch_feed_with_retry(self, topic_id: int) -> list[dict]:
        retry_delays = [0, 300, 600, 1200]
        base_feed_limit = 10

        for attempt, delay in enumerate(retry_delays):
            feed_limit = max(1, base_feed_limit - attempt * 2)

            if delay > 0:
                logger.info(f"社区 {topic_id} 第 {attempt} 次重试，等待 {delay//60} 分钟后，获取数量减至 {feed_limit}...")
                await asyncio.sleep(delay)

            feed_items = await self._fetch_feed(topic_id, feed_limit=feed_limit)
            if feed_items:
                logger.info(f"社区 {topic_id} 获取到 {len(feed_items)} 条 feed")
                return feed_items

            logger.warning(f"社区 {topic_id} 返回空列表（第 {attempt+1}/{len(retry_delays)} 次尝试，本次获取 {feed_limit} 条）")

        logger.error(f"社区 {topic_id} 多次重试后仍返回空列表，放弃本次获取")
        return []

    async def _fetch_and_process(self):
        async with self._lock:
            logger.info("开始执行刷取任务")

            all_feed_items = []
            for i, topic_id in enumerate(self.feed_topic_ids):
                logger.info(f"正在检索社区 {topic_id} ({i+1}/{len(self.feed_topic_ids)})")
                feed_items = await self._fetch_feed_with_retry(topic_id)
                if feed_items:
                    all_feed_items.extend(feed_items)

                if i < len(self.feed_topic_ids) - 1:
                    wait_min = self.topic_fetch_interval_minutes
                    logger.info(f"等待 {wait_min} 分钟后检索下一个社区...")
                    await asyncio.sleep(wait_min * 60)

            if all_feed_items:
                await self._process_new_posts(all_feed_items)
            logger.info("刷取任务完成")

    # ==================== 后台循环 ====================

    async def _background_loop(self):
        INTERVAL = self.interval_hours * 3600
        while True:
            try:
                await asyncio.wait_for(self._fetch_event.wait(), timeout=INTERVAL)
                self._fetch_event.clear()
                await self._fetch_and_process()
            except asyncio.TimeoutError:
                await self._fetch_and_process()
            except Exception as e:
                logger.error(f"后台循环异常: {e}")

    async def _nightly_fetch_loop(self):
        while True:
            try:
                now_bj = datetime.now(timezone(timedelta(hours=8)))
                target = now_bj.replace(hour=22, minute=10, second=0, microsecond=0)
                if target <= now_bj:
                    target = target + timedelta(days=1)

                wait_seconds = (target - now_bj).total_seconds()
                logger.info(f"定时拉取: 等待 {wait_seconds/3600:.1f} 小时到 {target.strftime('%Y-%m-%d %H:%M:%S')} 北京时间")
                await asyncio.sleep(wait_seconds)

                logger.info("执行每晚22:10定时拉取")
                await self._fetch_and_process()
            except Exception as e:
                logger.error(f"定时拉取异常: {e}")
                await asyncio.sleep(60)

    async def _manual_trigger(self):
        self._fetch_event.set()

    # ==================== LLM 分析 ====================

    async def _llm_analysis_loop(self):
        if not self.llm_analysis_enabled:
            logger.info("LLM 分析已禁用，跳过定时任务")
            return

        while True:
            try:
                now_bj = datetime.now(timezone(timedelta(hours=8)))

                try:
                    hour, minute = map(int, self.llm_analysis_time.split(":"))
                except (ValueError, AttributeError):
                    logger.error(f"LLM 分析时间格式错误: {self.llm_analysis_time}，使用默认 22:30")
                    hour, minute = 22, 30

                target = now_bj.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now_bj:
                    target = target + timedelta(days=1)

                wait_seconds = (target - now_bj).total_seconds()
                logger.info(f"LLM 分析定时: 等待 {wait_seconds/3600:.1f} 小时到 {target.strftime('%Y-%m-%d %H:%M:%S')} 北京时间")
                await asyncio.sleep(wait_seconds)

                logger.info("执行每日 LLM 帖子分析")
                await self._run_llm_analysis()
            except Exception as e:
                logger.error(f"LLM 分析定时循环异常: {e}")
                await asyncio.sleep(60)

    async def _run_llm_analysis(self):
        try:
            window_start, window_end = get_analysis_window()
            logger.info(f"LLM 分析窗口(固定): {ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)}")

            existing = self.llm_analyzer.db.get_existing_analysis_count(window_start)
            if existing > 0:
                logger.info(f"窗口内已有 {existing} 条分析记录，跳过重复分析")
                return

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT link_id, daily_no, title, create_at, userid, username, content, image_urls "
                "FROM posts WHERE window_start = ? ORDER BY daily_no",
                (window_start,)
            )
            rows = cur.fetchall()
            conn.close()

            if not rows:
                logger.info("今日没有帖子需要分析")
                return

            posts = []
            for link_id, daily_no, title, create_at, userid, username, content, image_urls in rows:
                image_paths = []
                if image_urls:
                    try:
                        image_paths = json.loads(image_urls)
                    except:
                        pass

                image_descriptions = self.image_analyzer.db.get_descriptions_for_post(link_id)

                posts.append({
                    "link_id": link_id,
                    "daily_no": daily_no,
                    "title": title or "(无标题)",
                    "username": username or f"用户{link_id}",
                    "userid": userid,
                    "create_at": create_at,
                    "create_at_str": ts_to_bj_str(create_at) if create_at else "未知",
                    "content": content or "",
                    "image_paths": image_paths,
                    "image_descriptions": image_descriptions,
                })

            logger.info(f"准备分析 {len(posts)} 个帖子")

            original_batch_size = self.llm_analyzer._batch_size
            self.llm_analyzer._batch_size = self.llm_analysis_batch_size

            try:
                success = await self.llm_analyzer.analyze_posts(window_start, posts)
                if success:
                    logger.info("LLM 分析全部完成")
                else:
                    logger.warning("LLM 分析部分失败")
            finally:
                self.llm_analyzer._batch_size = original_batch_size

        except Exception as e:
            logger.error(f"执行 LLM 分析失败: {e}")

    # ==================== 晚报（核心修改）====================

    async def _evening_report_loop(self):
        if not self.evening_report_enabled:
            logger.info("晚报已禁用，跳过定时任务")
            return

        while True:
            try:
                now_bj = datetime.now(timezone(timedelta(hours=8)))
                try:
                    hour, minute = map(int, self.evening_report_time.split(":"))
                except (ValueError, AttributeError):
                    logger.error(f"晚报时间格式错误: {self.evening_report_time}，使用默认 23:00")
                    hour, minute = 23, 0

                target = now_bj.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now_bj:
                    target = target + timedelta(days=1)

                wait_seconds = (target - now_bj).total_seconds()
                logger.info(f"晚报定时: 等待 {wait_seconds/3600:.1f} 小时到 {target.strftime('%Y-%m-%d %H:%M:%S')} 北京时间")
                await asyncio.sleep(wait_seconds)

                logger.info("执行每日晚报生成")
                await self._generate_and_save_evening_report(send=self.evening_report_auto_send)
            except Exception as e:
                logger.error(f"晚报定时循环异常: {e}")
                await asyncio.sleep(60)

    async def _generate_and_save_evening_report(self, send: bool = False):
        """
        生成晚报并保存到本地。如果 send=True 且配置了目标群，则发送到群里。
        
        格式由 evening_report_format 决定：
        - "image": 渲染为 PNG 图片，保存到本地，发送时发图片
        - "html": 保存为 HTML 文件到本地，发送时发文件
        """
        try:
            window_start, window_end = get_analysis_window()

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT p.daily_no, p.title, p.username, p.userid, p.avatar,
                       p.create_at, p.content, p.image_urls,
                       l.comment
                FROM posts p
                LEFT JOIN llm_analyses l ON p.link_id = l.link_id AND l.window_start = p.window_start
                WHERE p.window_start = ?
                ORDER BY p.daily_no
            """, (window_start,))
            rows = cur.fetchall()
            conn.close()

            if not rows:
                logger.info("今日没有帖子数据，跳过晚报生成")
                return

            posts = []
            for row in rows:
                posts.append({
                    "daily_no": row[0],
                    "title": row[1],
                    "username": row[2],
                    "userid": row[3],
                    "avatar": row[4],
                    "create_at_str": ts_to_bj_str(row[5]) if row[5] else "未知",
                    "content": row[6],
                    "image_paths": row[7],
                    "comment": row[8] or "暂无评论",
                })

            report_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y年%m月%d日")
            html_content = self.report_generator.generate_evening_report(
                posts=posts,
                issue_no=1,
                report_date=report_date,
                community_name="庭院社区",
                theme="default"
            )

            # 总是保存 HTML（作为备份）
            html_path = self.report_generator.save_report(html_content)
            logger.info(f"晚报 HTML 已保存: {html_path}")

            if self.evening_report_format == "html":
                # HTML 模式：保存文件，可选发送
                if send and self.evening_report_target_group:
                    await self._send_file_to_group(self.evening_report_target_group, html_path)
            else:
                # IMAGE 模式：渲染为 PNG，保存，可选发送
                image_url = await self._render_evening_report_image(html_content)
                if image_url:
                    # 保存图片到本地
                    if image_url.startswith("base64://"):
                        img_data = base64.b64decode(image_url[9:])
                        image_path = self.report_generator.save_image(img_data)
                    else:
                        image_path = image_url
                    logger.info(f"晚报 PNG 已保存: {image_path}")
                    
                    if send and self.evening_report_target_group:
                        await self._send_image_to_group(self.evening_report_target_group, image_path)
                else:
                    logger.error("晚报图片渲染失败，已保存 HTML 文件")

        except Exception as e:
            logger.error(f"生成晚报失败: {e}", exc_info=True)

    async def _render_evening_report_image(self, html_content: str) -> Optional[str]:
        """
        使用 AstrBot 内置的 html_render 将 HTML 渲染为图片
        """
        try:
            logger.info("开始调用 AstrBot T2I 渲染...")

            # AstrBot 4.x 的 html_render 参数：
            # html_render(template_str, data_dict, options={})
            # 但我们的 HTML 已经完整包含了所有数据，所以 data_dict 传空
            options = {
                "type": "png",
                "full_page": True,      # 捕获完整页面
                "omit_background": False,
            }

            # 调用继承自 Star 的 html_render 方法
            image_url = await self.html_render(html_content, {}, options=options)

            if not image_url:
                logger.error("html_render 返回空结果")
                return None

            logger.info(f"T2I 渲染成功: {image_url[:50]}...")
            return image_url

        except Exception as e:
            logger.error(f"渲染晚报图片失败: {e}", exc_info=True)
            return None

    # ==================== OneBot 11 发送方法 ====================

    async def _send_image_to_group(self, group_id: str, image_path: str):
        """发送图片到指定群（OneBot 11）"""
        try:
            logger.info(f"发送晚报图片到群 {group_id}")
            
            # 构造消息链
            from astrbot.api.message_components import Image, Plain
            chain = [
                Plain("📰 今日庭院社区晚报"),
                Image.fromFileSystem(image_path)
            ]
            
            # 使用 AstrBot 的消息发送接口
            # 方法1: 通过 event（如果当前有 event 上下文）
            # 方法2: 通过 context 的 send_message
            await self.context.send_message(group_id, chain)
            logger.info(f"晚报图片已发送到群 {group_id}")
            
        except Exception as e:
            logger.error(f"发送晚报图片到群 {group_id} 失败: {e}")

    async def _send_file_to_group(self, group_id: str, file_path: str):
        """发送文件到指定群（OneBot 11）"""
        try:
            logger.info(f"发送晚报 HTML 文件到群 {group_id}")
            
            # OneBot 11 发送群文件
            # 需要通过适配器直接调用
            adapter = self._get_onebot_adapter()
            if adapter and hasattr(adapter, "upload_group_file"):
                await adapter.upload_group_file(
                    group_id=group_id,
                    file_path=file_path,
                    file_name=f"庭院社区晚报_{datetime.now().strftime('%Y%m%d')}.html"
                )
                logger.info(f"晚报 HTML 文件已发送到群 {group_id}")
            else:
                #  fallback: 发送文本链接
                from astrbot.api.message_components import Plain
                chain = [Plain(f"📰 晚报 HTML 文件已生成，路径: {file_path}")]
                await self.context.send_message(group_id, chain)
                
        except Exception as e:
            logger.error(f"发送晚报文件到群 {group_id} 失败: {e}")

    def _get_onebot_adapter(self):
        """获取 OneBot 适配器"""
        try:
            # 遍历所有适配器找 OneBot
            for adapter in self.context.platforms:
                if hasattr(adapter, "upload_group_file") or "onebot" in str(type(adapter)).lower():
                    return adapter
            return None
        except Exception:
            return None

    # ==================== 指令 ====================

    @filter.command("刷取新内容")
    async def cmd_manual_fetch(self, event: AstrMessageEvent):
        yield event.plain_result("✅ 已触发刷取任务，请稍后查看结果。")
        await self._manual_trigger()

    @filter.command("重置今日")
    async def cmd_reset_today(self, event: AstrMessageEvent):
        window_start, window_end = get_today_window()

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()

        cur.execute(
            "SELECT link_id, daily_no FROM posts WHERE window_start = ? ORDER BY daily_no",
            (window_start,)
        )
        existing_posts = cur.fetchall()
        conn.close()

        if not existing_posts:
            yield event.plain_result("📭 当前时间窗口内还没有帖子，无法重置。")
            return

        count = len(existing_posts)
        yield event.plain_result(f"🔄 开始重置今日 {count} 条帖子，逐个重新拉取详情并覆盖...")

        asyncio.create_task(self._reset_today_posts(existing_posts, window_start))

    async def _reset_today_posts(self, existing_posts: list[tuple[int, int]], window_start: int):
        success_count = 0
        fail_count = 0

        for idx, (link_id, old_daily_no) in enumerate(existing_posts):
            logger.info(f"重置进度 {idx+1}/{len(existing_posts)}: 重新拉取 link_id={link_id} (原编号 #{old_daily_no})")

            detail = await self._fetch_link_detail(link_id)
            if not detail:
                logger.warning(f"重置失败 link_id={link_id}，拉取详情失败")
                fail_count += 1
                continue

            real_create_at = detail.get("create_at", 0)
            real_create_str = detail.get("create_at_str", "")

            window_end = window_start + 24 * 3600
            in_window = window_start <= real_create_at < window_end

            content_raw = detail.get("content", "")
            content_text, image_urls = self._parse_content(content_raw)

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM image_analyses WHERE link_id = ?", (link_id,))
            conn.commit()
            conn.close()

            saved_images = []
            for i, img_url in enumerate(image_urls):
                ext = ".png"
                if ".jpg" in img_url or ".jpeg" in img_url:
                    ext = ".jpg"
                elif ".webp" in img_url:
                    ext = ".webp"
                filename = f"{link_id}_{i}{ext}"
                saved = await self._download_image(img_url, filename)
                if saved:
                    saved_images.append(str(saved))

            if saved_images:
                logger.info(f"开始重新分析 link_id={link_id} 的 {len(saved_images)} 张图片")
                await self.image_analyzer.analyze_images(link_id, saved_images)

            topics_list = detail.get("topics", [])
            topics_names = [t.get("name", "") for t in topics_list if isinstance(t, dict) and t.get("name")]
            topics_str = json.dumps(topics_names, ensure_ascii=False)

            conn = sqlite3.connect(self.db_path)
            self._ensure_table_schema(conn)
            cur = conn.cursor()

            if in_window:
                cur.execute("""
                    INSERT OR REPLACE INTO posts
                    (link_id, daily_no, window_start, title, create_at, userid, username, avatar, topics, content, image_urls, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    link_id, old_daily_no, window_start,
                    detail.get("title", ""), real_create_at,
                    detail.get("userid", 0), detail.get("username", ""), detail.get("avatar", ""),
                    topics_str, content_text, json.dumps(saved_images, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat()
                ))
                logger.info(f"✅ 帖子重置成功: daily_no=#{old_daily_no}, link_id={link_id}, 发布时间={real_create_str}, 作者={detail.get('username', '')}")
            else:
                cur.execute("""
                    INSERT OR REPLACE INTO posts
                    (link_id, daily_no, window_start, title, create_at, userid, username, avatar, topics, content, image_urls, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    link_id, None, None,
                    detail.get("title", ""), real_create_at,
                    detail.get("userid", 0), detail.get("username", ""), detail.get("avatar", ""),
                    topics_str, content_text, json.dumps(saved_images, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat()
                ))
                logger.info(f"📌 帖子已移出今日窗口: link_id={link_id}, 发布时间={real_create_str}")

            conn.commit()
            conn.close()
            success_count += 1

            if idx < len(existing_posts) - 1:
                wait_sec = self.content_fetch_interval_seconds
                logger.info(f"等待 {wait_sec} 秒后处理下一个...")
                await asyncio.sleep(wait_sec)

        logger.info(f"重置任务完成: 成功 {success_count}/{len(existing_posts)}, 失败 {fail_count}")

    @filter.command("今日帖子")
    async def cmd_today_posts(self, event: AstrMessageEvent):
        window_start, window_end = get_today_window()

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no, title, create_at, userid, username, avatar, topics, content "
            "FROM posts WHERE window_start = ? ORDER BY daily_no",
            (window_start,)
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            yield event.plain_result("📭 当前时间窗口内还没有拉取到帖子。")
            return

        lines = [f"📋 今日帖子列表（{ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)}）：\n"]
        for link_id, daily_no, title, create_at, userid, username, avatar, topics, content in rows:
            dt_str = ts_to_bj_str(create_at)
            title_display = title if title else "(无标题)"
            author_display = username if username else f"用户{userid}"

            content_cleaned = clean_html_tags(content) if content else ""
            content_display = content_cleaned or "无内容"

            topics_display = ""
            if topics:
                try:
                    topics_list = json.loads(topics)
                    topics_display = " ".join([f"#{t}" for t in topics_list if t])
                except:
                    pass

            lines.append(
                f"━━━━━━━━━━━━━━\n"
                f"📌 编号: #{daily_no}\n"
                f"   ID: {link_id}\n"
                f"   标题: {title_display}\n"
                f"   作者: {author_display}\n"
                f"   时间: {dt_str}\n"
                f"   标签: {topics_display}\n"
                f"   内容:\n{content_display}\n"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("今日")
    async def cmd_today_detail(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /今日 <帖子编号>\n例如: /今日 1")
            return

        try:
            daily_no = int(parts[1])
        except ValueError:
            yield event.plain_result("❌ 帖子编号必须是数字")
            return

        window_start, window_end = get_today_window()

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no, title, create_at, userid, username, avatar, topics, content, image_urls "
            "FROM posts WHERE window_start = ? AND daily_no = ?",
            (window_start, daily_no)
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            yield event.plain_result(f"❌ 今日没有找到编号为 #{daily_no} 的帖子")
            return

        link_id, daily_no, title, create_at, userid, username, avatar, topics, content, image_urls = row
        dt_str = ts_to_bj_str(create_at)
        title_display = title if title else "(无标题)"
        author_display = username if username else f"用户{userid}"

        content_cleaned = clean_html_tags(content) if content else "无内容"

        topics_display = ""
        if topics:
            try:
                topics_list = json.loads(topics)
                topics_display = " ".join([f"#{t}" for t in topics_list if t])
            except:
                pass

        text_part = (
            f"📌 帖子详情 [编号 #{daily_no}]\n"
            f"━━━━━━━━━━━━━━\n"
            f"ID: {link_id}\n"
            f"标题: {title_display}\n"
            f"作者: {author_display}\n"
            f"时间: {dt_str}\n"
            f"标签: {topics_display}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{content_cleaned}"
        )

        chain = [Comp.Plain(text_part)]

        if image_urls:
            try:
                images = json.loads(image_urls)
                for img_path in images:
                    p = Path(img_path)
                    if p.exists():
                        chain.append(Comp.Image.fromFileSystem(str(p)))
                    else:
                        logger.warning(f"图片文件不存在: {img_path}")
            except Exception as e:
                logger.error(f"加载图片失败: {e}")

        yield event.chain_result(chain)

    @filter.command("登录")
    async def cmd_login(self, event: AstrMessageEvent):
        yield event.plain_result("⏳ 正在启动二维码登录（有效期120秒），请稍候...")

        args = [
            "src/main.py",
            "--login-qr",
            "--qr-timeout-seconds", "120",
            "--qr-poll-interval", "1"
        ]
        result = await self._run_command(args)

        if not result["success"]:
            err = result["stderr"][:500] if result["stderr"] else "未知错误"
            yield event.plain_result(f"❌ 登录执行失败：\n{err}")
            return

        qr_path = self.program_path / "qrcode.png"
        if qr_path.exists():
            yield event.image_result(str(qr_path))
            yield event.plain_result(
                "📸 请使用小黑盒APP扫描二维码进行登录。\n"
                "（登录成功后 cookie 已保存，可使用 /今日帖子 测试）"
            )
        else:
            yield event.plain_result("⚠️ 未生成二维码图片，请检查程序日志。")

    @filter.command("分析今日帖子")
    async def cmd_analyze_today(self, event: AstrMessageEvent):
        window_start, window_end = get_analysis_window()
        yield event.plain_result(
            f"🤖 正在启动 LLM 分析（窗口: {ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)}），请稍候..."
        )

        async def _run_with_feedback():
            try:
                await self._run_llm_analysis()
                logger.info("手动 LLM 分析任务完成")
            except Exception as e:
                logger.error(f"手动 LLM 分析任务失败: {e}")

        asyncio.create_task(_run_with_feedback())

    @filter.command("今日分析")
    async def cmd_today_analysis(self, event: AstrMessageEvent):
        try:
            window_start, window_end = get_analysis_window()
            report = await self.llm_analyzer.get_report(window_start)

            if not report:
                yield event.plain_result("📭 今日还没有 LLM 分析报告，请先执行 /分析今日帖子")
                return

            lines = [f"📊 帖子 LLM 分析评论（{ts_to_bj_str(window_start)} ~ {ts_to_bj_str(window_end)}）\n"]

            for item in report:
                lines.append(
                    f"━━━━━━━━━━━━━━\n"
                    f"📌 编号: #{item['daily_no']} | {item['title']}\n"
                    f"   作者: {item['username']}\n"
                    f"   📝 AI评论: {item.get('comment', 'N/A')}\n"
                )

            lines.append(f"\n📈 共 {len(report)} 条评论")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"获取分析报告失败: {e}")
            yield event.plain_result(f"❌ 获取分析报告失败: {e}")

    @filter.command("生成晚报")
    async def cmd_generate_report(self, event: AstrMessageEvent):
        """
        手动生成晚报。
        格式由配置 evening_report_format 决定：
        - image: 生成 PNG 图片，保存到本地，同时发送预览到当前群
        - html: 生成 HTML 文件，保存到本地，同时发送文件到当前群
        """
        window_start, window_end = get_analysis_window()

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT p.daily_no, p.title, p.username, p.userid, p.avatar,
                   p.create_at, p.content, p.image_urls,
                   l.comment
            FROM posts p
            LEFT JOIN llm_analyses l ON p.link_id = l.link_id AND l.window_start = p.window_start
            WHERE p.window_start = ?
            ORDER BY p.daily_no
        """, (window_start,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            yield event.plain_result("📭 今日没有帖子数据，无法生成晚报")
            return

        posts = []
        for row in rows:
            posts.append({
                "daily_no": row[0],
                "title": row[1],
                "username": row[2],
                "userid": row[3],
                "avatar": row[4],
                "create_at_str": ts_to_bj_str(row[5]) if row[5] else "未知",
                "content": row[6],
                "image_paths": row[7],
                "comment": row[8] or "暂无评论",
            })

        report_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y年%m月%d日")
        html_content = self.report_generator.generate_evening_report(
            posts=posts,
            issue_no=1,
            report_date=report_date,
            community_name="庭院社区",
            theme="default"
        )

        # 总是保存 HTML
        html_path = self.report_generator.save_report(html_content)
        yield event.plain_result(f"✅ 晚报 HTML 已保存到本地\n📄 {html_path}")

        if self.evening_report_format == "html":
            # HTML 模式：发送 HTML 文件到当前群
            yield event.plain_result("📎 正在发送 HTML 文件...")
            try:
                # 通过 event 发送文件
                # 注意：AstrBot 的 event 发送文件可能需要适配器支持
                # 这里尝试发送图片组件（如果平台支持文件消息）
                yield event.plain_result(f"文件已保存至: {html_path}")
            except Exception as e:
                yield event.plain_result(f"❌ 发送文件失败: {e}")
        else:
            # IMAGE 模式：渲染 PNG 并发送
            yield event.plain_result("🎨 正在渲染 PNG 图片，请稍候...")
            image_url = await self._render_evening_report_image(html_content)

            if image_url:
                yield event.plain_result("✅ 图片渲染成功！正在发送...")
                if image_url.startswith("base64://"):
                    img_data = base64.b64decode(image_url[9:])
                    tmp_path = self.report_generator.save_image(img_data)
                    # 保存到本地后发送
                    yield event.image_result(tmp_path)
                    yield event.plain_result(f"📷 图片已保存到本地: {tmp_path}")
                else:
                    yield event.image_result(image_url)
            else:
                yield event.plain_result("❌ 图片渲染失败，但 HTML 文件已保存到本地")

    # ==================== 生命周期 ====================

    async def terminate(self):
        for task_name, task in [
            ("bg_task", getattr(self, '_bg_task', None)),
            ("nightly_task", getattr(self, '_nightly_task', None)),
            ("llm_analysis_task", getattr(self, '_llm_analysis_task', None)),
            ("evening_report_task", getattr(self, '_evening_report_task', None)),
        ]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info(f"已取消任务: {task_name}")