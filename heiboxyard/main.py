import asyncio
import json
import sqlite3
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import aiohttp

# AstrBot 4.x API
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

# 插件配置默认值 - 使用相对插件目录的路径
DEFAULT_PROGRAM_PATH = "heibox-comment-bot-master"


def clean_html_tags(text: str) -> str:
    """清洗文本中的 <xxx> 标签"""
    if not text:
        return text
    cleaned = re.sub(r'<[^>]+>', '', text)
    return cleaned


@register("heiboxyard", "YourName", "小黑盒帖子自动拉取插件", "1.0.0", "https://github.com/your/repo")
class HeiboxYard(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context

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

        self._fetch_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._bg_task = asyncio.create_task(self._background_loop())
        self._nightly_task = asyncio.create_task(self._nightly_fetch_loop())

    def _ensure_table_schema(self, conn):
        """确保表结构完整，自动修复缺失字段"""
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
        if "topics" not in existing_cols:
            migrations.append("ALTER TABLE posts ADD COLUMN topics TEXT")

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

        # 帖子表
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='posts'")
        table_exists = cur.fetchone() is not None

        if not table_exists:
            cur.execute("""
                CREATE TABLE posts (
                    link_id INTEGER PRIMARY KEY,
                    daily_no INTEGER,
                    title TEXT,
                    create_at INTEGER,
                    userid INTEGER,
                    username TEXT,
                    topics TEXT,
                    content TEXT,
                    image_urls TEXT,
                    fetched_at TEXT
                )
            """)
            cur.execute("CREATE INDEX idx_posts_date ON posts(create_at)")
            logger.info("帖子表初始化完成")
        else:
            self._ensure_table_schema(conn)
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_posts_date'")
            if not cur.fetchone():
                cur.execute("CREATE INDEX idx_posts_date ON posts(create_at)")

        # 用户表 - 缓存用户信息
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                userid INTEGER PRIMARY KEY,
                username TEXT,
                avatar TEXT,
                updated_at TEXT
            )
        """)

        conn.commit()
        conn.close()

    def _get_next_daily_no(self, conn, date_timestamp: int) -> int:
        """获取指定日期的下一个 daily_no"""
        cur = conn.cursor()
        today_start = int(datetime.fromtimestamp(date_timestamp, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        today_end = today_start + 86400
        cur.execute(
            "SELECT MAX(daily_no) FROM posts WHERE create_at >= ? AND create_at < ?",
            (today_start, today_end)
        )
        result = cur.fetchone()[0]
        return (result or 0) + 1

    async def _download_image(self, url: str, filename: str) -> Optional[Path]:
        """下载图片到本地"""
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

    async def _run_command(self, args: list[str]) -> dict:
        """运行子进程"""
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

    async def _fetch_feed(self) -> list[dict]:
        args = [
            "src/main.py",
            "--get-feed-ids",
            "--feed-topic-id", "486620",
            "--feed-limit", "10",
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

    async def _fetch_content(self, link_id: int) -> Optional[tuple[str, list[str]]]:
        """获取帖子内容"""
        args = ["src/main.py", "--get-post-content", "--link-id", str(link_id)]
        result = await self._run_command(args)
        if not result["success"]:
            logger.error(f"拉取内容失败 link_id={link_id}: {result['stderr']}")
            return None
        try:
            blocks = json.loads(result["stdout"])
            text_parts, image_urls = [], []
            for block in blocks:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "img":
                    url = block.get("url")
                    if url:
                        image_urls.append(url)
            return "\n".join(text_parts).strip(), image_urls
        except json.JSONDecodeError as e:
            logger.error(f"解析内容 JSON 失败 link_id={link_id}: {e}")
            return None

    async def _fetch_owner_info(self, link_id: int) -> Optional[dict]:
        """获取帖主信息 - 通过 link_owner_simple.py 脚本"""
        script_path = self.program_path / "src" / "link_owner_simple.py"
        if not script_path.exists():
            script_path = self.program_path / "link_owner_simple.py"

        if not script_path.exists():
            logger.warning(f"link_owner_simple.py 不存在，已查找: {self.program_path / 'src' / 'link_owner_simple.py'}")
            return None

        args = [str(script_path), "--link-id", str(link_id)]
        result = await self._run_command(args)
        if not result["success"]:
            logger.warning(f"获取帖主信息失败 link_id={link_id}: {result['stderr'][:200]}")
            return None
        try:
            data = json.loads(result["stdout"])
            if "error" in data:
                logger.warning(f"获取帖主信息返回错误 link_id={link_id}: {data['error']}")
                return None
            return {
                "userid": data.get("userid"),
                "username": data.get("username"),
                "avatar": data.get("avatar")
            }
        except Exception as e:
            logger.warning(f"解析帖主信息失败 link_id={link_id}: {e}")
            return None

    async def _update_user_cache(self, userid: int, username: str, avatar: str):
        """更新用户缓存表"""
        if not userid:
            return
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users (userid, username, avatar, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(userid) DO UPDATE SET
                    username = excluded.username,
                    avatar = excluded.avatar,
                    updated_at = excluded.updated_at""",
            (userid, username or "", avatar or "", datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
        logger.info(f"用户缓存更新: userid={userid}, username={username}")

    async def _get_cached_username(self, userid: int) -> Optional[str]:
        """从缓存获取用户名"""
        if not userid:
            return None
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE userid = ?", (userid,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    async def _process_new_posts(self, feed_items: list[dict]):
        if not feed_items:
            return

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()

        # 先检查哪些帖子需要处理
        new_items = []
        for item in feed_items:
            link_id = item["link_id"]
            cur.execute("SELECT link_id, content, image_urls, username FROM posts WHERE link_id = ?", (link_id,))
            existing = cur.fetchone()

            if existing is None:
                new_items.append({"item": item, "status": "new"})
            else:
                _, content, image_urls, username = existing
                missing = []
                if not content:
                    missing.append("content")
                if not image_urls:
                    missing.append("images")
                if not username:
                    missing.append("username")
                if missing:
                    new_items.append({"item": item, "status": "update", "missing": missing})
                    logger.info(f"帖子 link_id={link_id} 缺失数据: {missing}，将重新拉取")

        conn.close()

        if not new_items:
            logger.info("没有新帖子或需要更新的帖子")
            return

        # 处理新帖子和需要更新的帖子
        for entry in new_items:
            item = entry["item"]
            status = entry["status"]
            link_id = item["link_id"]
            title = item.get("title", "")
            create_at = item["create_at"]
            userid = item.get("userid", 0)

            topics_list = item.get("topics", [])
            topics_names = [t.get("name", "") for t in topics_list if t.get("name")]
            topics_str = json.dumps(topics_names, ensure_ascii=False)

            conn = sqlite3.connect(self.db_path)
            self._ensure_table_schema(conn)
            cur = conn.cursor()

            if status == "new":
                daily_no = self._get_next_daily_no(conn, create_at)
                cur.execute(
                    "INSERT OR REPLACE INTO posts (link_id, daily_no, title, create_at, userid, topics) VALUES (?, ?, ?, ?, ?, ?)",
                    (link_id, daily_no, title, create_at, userid, topics_str)
                )
                logger.info(f"新帖子入库: daily_no={daily_no}, link_id={link_id}, title={title}")
            else:
                cur.execute("SELECT daily_no FROM posts WHERE link_id = ?", (link_id,))
                row = cur.fetchone()
                daily_no = row[0] if row else self._get_next_daily_no(conn, create_at)
                cur.execute(
                    "UPDATE posts SET title = ?, create_at = ?, userid = ?, topics = ? WHERE link_id = ?",
                    (title, create_at, userid, topics_str, link_id)
                )
                logger.info(f"更新帖子: daily_no={daily_no}, link_id={link_id}, 缺失: {entry.get('missing', [])}")

            conn.commit()
            conn.close()

        # 获取帖主信息并更新用户缓存
        processed_userids = set()
        for entry in new_items:
            item = entry["item"]
            link_id = item["link_id"]
            userid = item.get("userid", 0)

            if userid and userid not in processed_userids:
                cached_name = await self._get_cached_username(userid)
                if cached_name:
                    conn = sqlite3.connect(self.db_path)
                    self._ensure_table_schema(conn)
                    cur = conn.cursor()
                    cur.execute("UPDATE posts SET username = ? WHERE userid = ?", (cached_name, userid))
                    conn.commit()
                    conn.close()
                    logger.info(f"使用缓存用户名: {cached_name} (userid={userid})")
                else:
                    owner_info = await self._fetch_owner_info(link_id)
                    if owner_info and owner_info.get("username"):
                        username = owner_info.get("username", "")
                        avatar = owner_info.get("avatar", "")
                        actual_userid = owner_info.get("userid", userid)

                        await self._update_user_cache(actual_userid, username, avatar)

                        conn = sqlite3.connect(self.db_path)
                        self._ensure_table_schema(conn)
                        cur = conn.cursor()
                        cur.execute("UPDATE posts SET username = ? WHERE userid = ?", (username, userid))
                        conn.commit()
                        conn.close()
                        logger.info(f"获取到帖主信息: {username} (userid={userid})")
                    else:
                        logger.warning(f"帖主信息获取失败: link_id={link_id}, userid={userid}")

                processed_userids.add(userid)
                await asyncio.sleep(1)

        # 拉取帖子内容（带间隔）
        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()

        link_ids = [entry["item"]["link_id"] for entry in new_items]
        placeholders = ",".join(["?"] * len(link_ids))
        cur.execute(
            f"SELECT link_id, daily_no FROM posts WHERE link_id IN ({placeholders}) AND (fetched_at IS NULL OR content IS NULL OR content = '') ORDER BY daily_no",
            link_ids
        )
        to_fetch = cur.fetchall()
        conn.close()

        if not to_fetch:
            logger.info("没有需要拉取内容的帖子")
            return

        logger.info(f"需要拉取内容的帖子数: {len(to_fetch)}")
        for idx, (link_id, daily_no) in enumerate(to_fetch):
            logger.info(f"({idx+1}/{len(to_fetch)}) 拉取内容 daily_no={daily_no}, link_id={link_id}")
            result = await self._fetch_content(link_id)
            if result:
                content, image_urls = result

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

                conn = sqlite3.connect(self.db_path)
                self._ensure_table_schema(conn)
                cur = conn.cursor()
                cur.execute(
                    "UPDATE posts SET content = ?, image_urls = ?, fetched_at = ? WHERE link_id = ?",
                    (content, json.dumps(saved_images, ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(), link_id)
                )
                conn.commit()
                conn.close()
                logger.info(f"成功拉取 daily_no={daily_no}, link_id={link_id}, 图片数={len(saved_images)}")
            else:
                logger.warning(f"拉取内容失败 link_id={link_id}")

            if idx < len(to_fetch) - 1:
                logger.info(f"等待 30 秒后拉取下一个...")
                await asyncio.sleep(30)

    async def _fetch_and_process(self):
        async with self._lock:
            logger.info("开始执行刷取任务")
            feed_items = await self._fetch_feed()
            if feed_items:
                await self._process_new_posts(feed_items)
            logger.info("刷取任务完成")

    async def _background_loop(self):
        """常规后台循环：每4小时拉取一次"""
        INTERVAL = 4 * 3600
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
        """每晚22:10定时拉取"""
        while True:
            try:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=22, minute=10, second=0, microsecond=0)
                if target <= now:
                    target = target + __import__('datetime').timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(f"定时拉取: 等待 {wait_seconds/3600:.1f} 小时到 {target.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                await asyncio.sleep(wait_seconds)

                logger.info("执行每晚22:10定时拉取")
                await self._fetch_and_process()
            except Exception as e:
                logger.error(f"定时拉取异常: {e}")
                await asyncio.sleep(60)

    async def _manual_trigger(self):
        self._fetch_event.set()

    # ==================== 指令 ====================

    @filter.command("刷取新内容")
    async def cmd_manual_fetch(self, event: AstrMessageEvent):
        yield event.plain_result("✅ 已触发刷取任务，请稍后查看结果。")
        await self._manual_trigger()

    @filter.command("重置今日")
    async def cmd_reset_today(self, event: AstrMessageEvent):
        """清空今日数据并重新拉取"""
        today_start = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM posts WHERE create_at >= ?", (today_start,))
        count = cur.fetchone()[0]

        if count > 0:
            cur.execute("DELETE FROM posts WHERE create_at >= ?", (today_start,))
            conn.commit()
            logger.info(f"已清空今日 {count} 条帖子数据")
        conn.close()

        yield event.plain_result(f"🗑️ 已清空今日 {count} 条数据，开始重新拉取...")
        asyncio.create_task(self._fetch_and_process())

    @filter.command("今日帖子")
    async def cmd_today_posts(self, event: AstrMessageEvent):
        today_start = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no, title, create_at, userid, username, topics, content "
            "FROM posts WHERE create_at >= ? ORDER BY daily_no",
            (today_start,)
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            yield event.plain_result("📭 今天还没有拉取到帖子。")
            return

        lines = ["📋 今日帖子列表：\n"]
        for link_id, daily_no, title, create_at, userid, username, topics, content in rows:
            dt = datetime.fromtimestamp(create_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
                f"   时间: {dt}\n"
                f"   标签: {topics_display}\n"
                f"   内容:\n{content_display}\n"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("今日")
    async def cmd_today_detail(self, event: AstrMessageEvent):
        """查看指定编号的今日帖子详情（文本+图片合并为一条消息链）"""
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

        today_start = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())

        conn = sqlite3.connect(self.db_path)
        self._ensure_table_schema(conn)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no, title, create_at, userid, username, topics, content, image_urls "
            "FROM posts WHERE create_at >= ? AND daily_no = ?",
            (today_start, daily_no)
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            yield event.plain_result(f"❌ 今日没有找到编号为 #{daily_no} 的帖子")
            return

        link_id, daily_no, title, create_at, userid, username, topics, content, image_urls = row
        dt = datetime.fromtimestamp(create_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
            f"时间: {dt}\n"
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
        """清除旧cookie并重新执行二维码登录，返回二维码图片"""
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

    async def terminate(self):
        """插件卸载/停用时调用"""
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        if self._nightly_task and not self._nightly_task.done():
            self._nightly_task.cancel()
            try:
                await self._nightly_task
            except asyncio.CancelledError:
                pass