"""
小黑盒帖子自动拉取插件 (重构版)
主入口：AstrBot Star 注册、指令处理、定时任务调度
核心改进：
- 所有帖子转移/调整操作使用 link_id 硬绑定级联同步
- 支持评论数据展示
- 修复数据失联问题
"""
import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig

from .utils import (
    clean_html_tags, get_current_window, get_current_window_no, get_window_by_no,
    ts_to_bj_str, parse_time_str, get_next_target_time, get_window_no_from_start,
    parse_daily_no, format_daily_no
)
from .post_manager import PostManager
from .at_fetcher_plugin import AtMessageFetcher
from .llm_analyzer import LLMPostAnalyzer
from .image_analyzer import ImagePostAnalyzer
from .memory_db import UserMemoryDB
from .report_generator import EveningReportGenerator
from .report_ai_summary import generate_ai_summary

DEFAULT_PROGRAM_PATH = "heibox-comment-bot-master"


@register("heiboxyard", "YourName", "小黑盒帖子自动拉取插件", "2.1.0", "https://github.com/your/repo")
class HeiboxYard(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        # ========== 基础配置 ==========
        self.interval_hours = self.config.get("interval_hours", 2)
        self.feed_topic_ids = self.config.get("feed_topic_ids", [486620])
        self.topic_fetch_interval_minutes = self.config.get("topic_fetch_interval_minutes", 5)
        self.content_fetch_interval_seconds = self.config.get("content_fetch_interval_seconds", 30)

        if isinstance(self.feed_topic_ids, str):
            try:
                self.feed_topic_ids = [int(x.strip()) for x in self.feed_topic_ids.split(",") if x.strip()]
            except ValueError:
                self.feed_topic_ids = [486620]
        elif not isinstance(self.feed_topic_ids, list):
            self.feed_topic_ids = [486620]

        # ========== LLM 分析配置 ==========
        self.llm_analysis_enabled = self.config.get("llm_analysis_enabled", True)
        self.llm_analysis_time = self.config.get("llm_analysis_time", "22:30")
        self.llm_provider_id = self.config.get("llm_provider_id", "")
        self.vision_provider_id = self.config.get("vision_provider_id", "")
        self.llm_analysis_batch_size = self.config.get("llm_analysis_batch_size", 8)

        # ========== 晚报配置 ==========
        self.evening_report_enabled = self.config.get("evening_report_enabled", True)
        self.evening_report_auto_send = self.config.get("evening_report_auto_send", False)
        self.evening_report_time = self.config.get("evening_report_time", "23:00")
        self.evening_report_format = self.config.get("evening_report_format", "image")
        self.evening_report_target_group = self.config.get("evening_report_target_group", "")

        # ========== @消息配置 ==========
        self.at_fetch_enabled = self.config.get("at_fetch_enabled", True)
        self.at_fetch_hours = self.config.get("at_fetch_hours", [4, 10, 16, 22])
        self.at_fetch_recent_hours = self.config.get("at_fetch_recent_hours", 6)

        logger.info(f"配置加载: interval_hours={self.interval_hours}, feed_topic_ids={self.feed_topic_ids}")
        logger.info(f"LLM分析: enabled={self.llm_analysis_enabled}, time={self.llm_analysis_time}")
        logger.info(f"晚报: enabled={self.evening_report_enabled}, auto_send={self.evening_report_auto_send}")
        logger.info(f"@消息: enabled={self.at_fetch_enabled}, hours={self.at_fetch_hours}")

        # ========== 路径初始化 ==========
        raw_path = self.plugin_config.get("program_path", DEFAULT_PROGRAM_PATH) if hasattr(self, 'plugin_config') else DEFAULT_PROGRAM_PATH
        plugin_dir = Path(__file__).parent.resolve()
        program_path = plugin_dir / raw_path
        if not program_path.exists():
            program_path = Path(raw_path).resolve()
        self.program_path = program_path
        self.plugin_dir = plugin_dir

        if not self.program_path.exists():
            logger.error(f"小黑盒程序路径不存在: {self.program_path}")

        data_dir = plugin_dir / "data"
        data_dir.mkdir(exist_ok=True)
        self.db_path = data_dir / "posts.db"
        self.image_dir = data_dir / "images"
        self.image_dir.mkdir(exist_ok=True)

        # ========== 子模块初始化 ==========
        self.post_manager = PostManager(
            db_path=self.db_path,
            image_dir=self.image_dir,
            program_path=self.program_path,
            content_fetch_interval_seconds=self.content_fetch_interval_seconds
        )

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

        self.at_fetcher = AtMessageFetcher(
            post_manager=self.post_manager,
            fetch_hours=self.at_fetch_hours,
            recent_hours=self.at_fetch_recent_hours,
            enabled=self.at_fetch_enabled
        )

        # ========== 任务管理 ==========
        self._lock = asyncio.Lock()
        self._tasks = []
        self._start_tasks()

    def _start_tasks(self):
        """启动所有后台任务"""
        self._tasks = [
            asyncio.create_task(self._background_loop(), name="bg_task"),
            asyncio.create_task(self._nightly_fetch_loop(), name="nightly_task"),
            asyncio.create_task(self._llm_analysis_loop(), name="llm_analysis_task"),
            asyncio.create_task(self._evening_report_loop(), name="evening_report_task"),
        ]
        self.at_fetcher.start()

    # ==================== Feed 拉取 ====================

    async def _fetch_feed_with_retry(self, topic_id: int) -> list[dict]:
        """带重试的 feed 拉取"""
        retry_delays = [0, 300, 600, 1200]
        base_feed_limit = 10

        for attempt, delay in enumerate(retry_delays):
            feed_limit = max(1, base_feed_limit - attempt * 2)
            if delay > 0:
                logger.info(f"社区 {topic_id} 第 {attempt} 次重试，等待 {delay//60} 分钟后获取 {feed_limit} 条...")
                await asyncio.sleep(delay)

            feed_items = await self.post_manager.fetch_feed(topic_id, feed_limit=feed_limit)
            if feed_items:
                logger.info(f"社区 {topic_id} 获取到 {len(feed_items)} 条 feed")
                return feed_items

            logger.warning(f"社区 {topic_id} 返回空列表（第 {attempt+1}/{len(retry_delays)} 次）")

        logger.error(f"社区 {topic_id} 多次重试后仍为空")
        return []

    async def _fetch_and_process_feed(self):
        """拉取并处理 feed 帖子"""
        async with self._lock:
            logger.info("开始执行 Feed 刷取任务")
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
                seen = set()
                link_ids = []
                for item in all_feed_items:
                    lid = item["link_id"]
                    if lid not in seen:
                        seen.add(lid)
                        link_ids.append(lid)
                await self.post_manager.process_posts(link_ids, source="feed")

            logger.info("Feed 刷取任务完成")

    # ==================== 后台循环 ====================

    async def _background_loop(self):
        INTERVAL = self.interval_hours * 3600
        await asyncio.sleep(30)
        logger.info(f"后台循环启动，首次拉取将在 30 秒后开始，之后每 {self.interval_hours} 小时执行一次")

        while True:
            try:
                await self._fetch_and_process_feed()
            except Exception as e:
                logger.error(f"后台拉取异常: {e}")

            logger.info(f"Feed 拉取完成，{self.interval_hours} 小时后进行下一次")
            await asyncio.sleep(INTERVAL)

    async def _nightly_fetch_loop(self):
        while True:
            try:
                target = get_next_target_time(22, 10)
                wait_seconds = (target - datetime.now(timezone(timedelta(hours=8)))).total_seconds()
                logger.info(f"定时拉取: 等待 {wait_seconds/3600:.1f} 小时到 {target.strftime('%Y-%m-%d %H:%M:%S')} 北京时间")
                await asyncio.sleep(wait_seconds)

                logger.info("执行每晚 22:10 定时拉取")
                await self._fetch_and_process_feed()
            except Exception as e:
                logger.error(f"定时拉取异常: {e}")
                await asyncio.sleep(60)

    async def _manual_trigger(self):
        """手动触发一次拉取"""
        logger.info("收到手动触发请求")
        for task in self._tasks:
            if task.get_name() == "bg_task" and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                new_task = asyncio.create_task(self._background_loop(), name="bg_task")
                self._tasks = [new_task if t.get_name() == "bg_task" else t for t in self._tasks]
                logger.info("已重新启动后台拉取任务，将立即执行")
                break

    # ==================== LLM 分析 ====================

    async def _llm_analysis_loop(self):
        if not self.llm_analysis_enabled:
            logger.info("LLM 分析已禁用")
            return

        while True:
            try:
                hour, minute = parse_time_str(self.llm_analysis_time)
                target = get_next_target_time(hour, minute)
                wait_seconds = (target - datetime.now(timezone(timedelta(hours=8)))).total_seconds()
                logger.info(f"LLM 分析定时: 等待 {wait_seconds/3600:.1f} 小时到 {target.strftime('%Y-%m-%d %H:%M:%S')}")
                await asyncio.sleep(wait_seconds)

                logger.info("执行每日 LLM 帖子分析")
                await self._run_llm_analysis()
            except Exception as e:
                logger.error(f"LLM 分析定时循环异常: {e}")
                await asyncio.sleep(60)

    async def _run_llm_analysis(self, window_no: str = None, force_reanalyze: bool = False) -> dict:
        """执行 LLM 分析（重构版：使用 link_id 硬绑定）"""
        accumulated_tokens = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }

        try:
            if window_no is None:
                window_no = get_current_window_no()

            window_start, window_end = get_window_by_no(window_no)
            logger.info(f"LLM 分析: 窗口={window_no}, window_start={ts_to_bj_str(window_start)}")

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT link_id, daily_no, title, create_at, userid, username, content, image_urls "
                "FROM posts WHERE date_str = ? ORDER BY daily_no",
                (window_no,)
            )
            rows = cur.fetchall()
            conn.close()

            if not rows:
                logger.info(f"窗口 {window_no} 没有帖子")
                return accumulated_tokens

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
                    "link_id": link_id, "daily_no": daily_no,
                    "title": title or "(无标题)", "username": username or f"用户{link_id}",
                    "userid": userid, "create_at": create_at,
                    "create_at_str": ts_to_bj_str(create_at) if create_at else "未知",
                    "content": content or "", "image_paths": image_paths,
                    "image_descriptions": image_descriptions,
                })

            existing_count = self.llm_analyzer.db.get_existing_analysis_count(window_start)

            if existing_count > 0 and force_reanalyze:
                logger.info(f"强制重新分析窗口 {window_no}，清除 {existing_count} 条旧记录")
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cur.execute("DELETE FROM llm_analyses WHERE window_start = ?", (window_start,))
                conn.commit()
                conn.close()

            logger.info(f"准备分析 {len(posts)} 个帖子")
            original_batch_size = self.llm_analyzer._batch_size
            self.llm_analyzer._batch_size = self.llm_analysis_batch_size

            try:
                if force_reanalyze or existing_count == 0:
                    success, batch_tokens = await self.llm_analyzer.analyze_posts(window_start, posts)
                    for key in accumulated_tokens:
                        accumulated_tokens[key] += batch_tokens.get(key, 0)

                filled, still_missing, fill_tokens = await self._ensure_all_analyses(window_start, posts)
                for key in accumulated_tokens:
                    accumulated_tokens[key] += fill_tokens.get(key, 0)

            finally:
                self.llm_analyzer._batch_size = original_batch_size

            logger.info(
                f"LLM 分析总token: total={accumulated_tokens['total_tokens']}, "
                f"prompt={accumulated_tokens['prompt_tokens']}, "
                f"completion={accumulated_tokens['completion_tokens']}"
            )
            return accumulated_tokens

        except Exception as e:
            logger.error(f"执行 LLM 分析失败: {e}")
            return accumulated_tokens

    async def _ensure_all_analyses(self, window_start: int, posts: list[dict]) -> tuple[int, int, dict]:
        """确保窗口内所有帖子都有 AI 评论"""
        accumulated_tokens = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }

        window_no = get_window_no_from_start(window_start)
        logger.info(f"🔍 检查窗口 {window_no} 的评论完整性...")

        filled, still_missing, fill_tokens = await self.llm_analyzer.fill_missing_analyses(window_start, posts)
        for key in accumulated_tokens:
            accumulated_tokens[key] += fill_tokens.get(key, 0)

        logger.info(f"📝 检查窗口 {window_no} 的总评...")
        await self._ensure_ai_summary(window_no, posts)
        logger.info(f"✅ 窗口 {window_no} 分析完整性检查完成")

        return filled, still_missing, accumulated_tokens

    async def _ensure_ai_summary(self, window_no: str, posts: list[dict]):
        """检查并确保窗口的总评已生成"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM llm_analyses WHERE daily_no LIKE ? || '-_%'",
                (window_no,)
            )
            analysis_count = cur.fetchone()[0]

            ws = get_window_by_no(window_no)[0]
            cur.execute(
                "SELECT comment FROM llm_analyses WHERE window_start = ? AND daily_no = ?",
                (ws, "SUMMARY")
            )
            existing_summary = cur.fetchone()
            conn.close()

            if analysis_count == 0:
                logger.warning(f"窗口 {window_no} 无分析记录，无法生成总评")
                return

            if existing_summary and existing_summary[0] and existing_summary[0].strip():
                logger.info(f"窗口 {window_no} 已有总评")
                return

            logger.info(f"📝 窗口 {window_no} 预生成总评...")
            ai_summary, summary_model, summary_tokens = await generate_ai_summary(
                self.context, posts, window_no, self.llm_provider_id
            )

            if ai_summary and ai_summary.strip():
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                window_start = get_window_by_no(window_no)[0]
                cur.execute("""
                    INSERT OR REPLACE INTO llm_analyses 
                    (window_start, daily_no, link_id, title, comment, analyzed_at, model_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    window_start, "SUMMARY", 0, "AI总评", 
                    ai_summary, 
                    datetime.now(timezone.utc).isoformat(),
                    summary_model or "unknown"
                ))
                conn.commit()
                conn.close()
                logger.info(f"✅ 窗口 {window_no} 总评预生成完成")
            else:
                logger.warning(f"窗口 {window_no} 总评生成失败")

        except Exception as e:
            logger.error(f"预生成总评失败: {e}")

    # ==================== 晚报生成 ====================

    async def _evening_report_loop(self):
        if not self.evening_report_enabled:
            logger.info("晚报已禁用")
            return

        while True:
            try:
                hour, minute = parse_time_str(self.evening_report_time)
                target = get_next_target_time(hour, minute)
                wait_seconds = (target - datetime.now(timezone(timedelta(hours=8)))).total_seconds()
                logger.info("晚报定时: 等待 " + str(round(wait_seconds/3600, 1)) + " 小时到 " + target.strftime('%Y-%m-%d %H:%M:%S'))
                await asyncio.sleep(wait_seconds)

                logger.info("执行每日晚报生成")
                analysis_tokens = await self._run_llm_analysis(force_reanalyze=False)
                await self._generate_and_save_evening_report(
                    send=self.evening_report_auto_send,
                    analysis_tokens=analysis_tokens
                )
            except Exception as e:
                logger.error("晚报定时循环异常: " + str(e))
                await asyncio.sleep(60)

    async def _generate_and_save_evening_report(self, window_no: str = None, send: bool = False,
                                                analysis_tokens: dict = None, summary_tokens: dict = None):
        """生成晚报"""
        import time
        start_time = time.time()

        try:
            if window_no is None:
                window_no = get_current_window_no()

            window_start, window_end = get_window_by_no(window_no)

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute("""
                SELECT p.daily_no, p.title, p.username, p.userid, p.avatar,
                       p.create_at, p.content, p.image_urls, l.comment, l.model_used
                FROM posts p
                LEFT JOIN llm_analyses l ON p.link_id = l.link_id
                WHERE p.date_str = ? ORDER BY p.daily_no
            """, (window_no,))
            rows = cur.fetchall()

            cur.execute("""
                SELECT COUNT(*) FROM llm_analyses 
                WHERE daily_no LIKE ? || '-_%'
            """, (window_no,))
            total_comments = cur.fetchone()[0]

            conn.close()

            if not rows:
                logger.info("窗口 " + window_no + " 没有帖子")
                return

            posts = []
            model_used_set = set()
            for row in rows:
                posts.append({
                    "daily_no": row[0], "title": row[1], "username": row[2],
                    "userid": row[3], "avatar": row[4],
                    "create_at_str": ts_to_bj_str(row[5]) if row[5] else "未知",
                    "content": row[6], "image_paths": row[7],
                    "comment": row[8] or "暂无评论",
                })
                if row[9]:
                    model_used_set.add(row[9])

            ai_summary, summary_model, summary_token_info = await generate_ai_summary(
                self.context, posts, window_no, self.llm_provider_id
            )

            elapsed = time.time() - start_time
            elapsed_str = str(round(elapsed, 1)) + "s"

            report_date = datetime.fromtimestamp(window_end, tz=timezone(timedelta(hours=8))).strftime("%Y年%m月%d日")

            all_models = set(model_used_set)
            if summary_model and summary_model != "unknown":
                all_models.add(summary_model)
            model_str = ", ".join(all_models) if all_models else "--"

            total_tokens = 0
            total_cache_hit = 0
            total_completion = 0

            if analysis_tokens:
                total_tokens += analysis_tokens.get("total_tokens", 0)
                total_cache_hit += analysis_tokens.get("prompt_cache_hit_tokens", 0)
                total_completion += analysis_tokens.get("completion_tokens", 0)

            if summary_token_info:
                total_tokens += summary_token_info.get("total_tokens", 0)
                total_cache_hit += summary_token_info.get("prompt_cache_hit_tokens", 0)
                total_completion += summary_token_info.get("completion_tokens", 0)

            tokens_str = f"{total_tokens}/{total_cache_hit}/{total_completion}" if total_tokens > 0 else "--"

            issue_no = self.report_generator.calculate_issue_no(window_no)
            generation_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

            html_content = self.report_generator.generate_evening_report(
                posts=posts, 
                issue_no=issue_no, 
                report_date=report_date,
                community_name="庭院社区", 
                theme="default",
                ai_summary=ai_summary,
                total_comments=total_comments,
                generation_time=generation_time,
                tokens_used=tokens_str,
                model_used=model_str,
            )

            html_path = self.report_generator.save_report(html_content, window_no=window_no)
            logger.info("晚报 HTML 已保存: " + html_path)

            if self.evening_report_format == "html":
                if send and self.evening_report_target_group:
                    await self._send_file_to_group(self.evening_report_target_group, html_path)
            else:
                image_url = await self._render_evening_report_image(html_content)
                if image_url:
                    if image_url.startswith("base64://"):
                        img_data = base64.b64decode(image_url[9:])
                        image_path = self.report_generator.save_image(img_data, window_no=window_no)
                    else:
                        image_path = image_url
                    logger.info("晚报 PNG 已保存: " + image_path)
                    if send and self.evening_report_target_group:
                        await self._send_image_to_group(self.evening_report_target_group, image_path)
                else:
                    logger.error("晚报图片渲染失败")

        except Exception as e:
            logger.error("生成晚报失败: " + str(e), exc_info=True)

    async def _render_evening_report_image(self, html_content: str) -> Optional[str]:
        try:
            logger.info("开始调用 AstrBot T2I 渲染...")
            options = {"type": "png", "full_page": True, "omit_background": False}
            image_url = await self.html_render(html_content, {}, options=options)
            if not image_url:
                logger.error("html_render 返回空结果")
                return None
            logger.info("T2I 渲染成功: " + image_url[:50] + "...")
            return image_url
        except Exception as e:
            logger.error("渲染晚报图片失败: " + str(e), exc_info=True)
            return None

    # ==================== 发送方法 ====================

    async def _send_image_to_group(self, group_id: str, image_path: str):
        try:
            from astrbot.api.message_components import Image, Plain
            chain = [Plain("📰 庭院社区晚报"), Image.fromFileSystem(image_path)]
            await self.context.send_message(group_id, chain)
            logger.info("晚报图片已发送到群 " + group_id)
        except Exception as e:
            logger.error("发送晚报图片失败: " + str(e))

    async def _send_file_to_group(self, group_id: str, file_path: str):
        try:
            adapter = self._get_onebot_adapter()
            if adapter and hasattr(adapter, "upload_group_file"):
                await adapter.upload_group_file(
                    group_id=group_id, file_path=file_path,
                    file_name="庭院社区晚报_" + datetime.now().strftime('%Y%m%d') + ".html"
                )
            else:
                from astrbot.api.message_components import Plain
                await self.context.send_message(group_id, [Plain("📰 晚报文件: " + file_path)])
        except Exception as e:
            logger.error("发送晚报文件失败: " + str(e))

    def _get_onebot_adapter(self):
        try:
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

    @filter.command("刷取at消息")
    async def cmd_manual_at_fetch(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 正在手动拉取 @消息（最近6小时），请稍候...")
        async def _run():
            count = await self.at_fetcher.manual_fetch()
            logger.info(f"手动 @消息拉取完成: {count} 个帖子")
        asyncio.create_task(_run())

    @filter.command("刷取当前窗口at")
    async def cmd_manual_at_fetch_window(self, event: AstrMessageEvent):
        window_start, window_end = get_current_window()
        start_bj = datetime.fromtimestamp(window_start, tz=timezone(timedelta(hours=8)))
        end_bj = datetime.fromtimestamp(window_end, tz=timezone(timedelta(hours=8)))
        start_str = start_bj.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_bj.strftime("%Y-%m-%d %H:%M:%S")
        window_no = get_current_window_no()

        yield event.plain_result(
            f"🔄 正在拉取当前窗口({window_no})的@消息\n"
            f"📅 时间范围: {start_str} ~ {end_str}"
        )

        async def _run():
            try:
                count = await self.at_fetcher.manual_fetch(start_time=start_str, end_time=end_str)
                logger.info(f"窗口 {window_no} @消息拉取完成: {count} 个帖子")
            except Exception as e:
                logger.error(f"窗口 @消息拉取失败: {e}")
        asyncio.create_task(_run())

    @filter.command("重置今日")
    async def cmd_reset_today(self, event: AstrMessageEvent):
        window_no = get_current_window_no()
        existing_posts = self.post_manager.get_posts_by_window_no(window_no)

        if not existing_posts:
            yield event.plain_result(f"📭 窗口 {window_no} 内还没有帖子")
            return

        count = len(existing_posts)
        yield event.plain_result(f"🔄 开始重置窗口 {window_no} 的 {count} 条帖子...")
        asyncio.create_task(self._reset_window_posts(existing_posts, window_no))

    async def _reset_window_posts(self, existing_posts: list[tuple[int, str]], window_no: str):
        window_start, window_end = get_window_by_no(window_no)
        success_count = 0
        reanalyzed_count = 0
        skipped_count = 0
        for idx, (link_id, old_daily_no) in enumerate(existing_posts):
            logger.info(f"重置进度 {idx+1}/{len(existing_posts)}: link_id={link_id}")

            detail = await self.post_manager.fetch_link_detail(link_id)
            if not detail:
                logger.warning(f"重置失败 link_id={link_id}")
                continue

            real_create_at = detail.get("create_at", 0)
            in_window = window_start <= real_create_at < window_end

            content_text, image_urls = self.post_manager.parse_content(detail.get("content", ""))
            self.post_manager.delete_image_analyses(link_id)
            saved_images = await self.post_manager.download_images(link_id, image_urls)
            if saved_images:
                await self.image_analyzer.analyze_images(link_id, saved_images)

            topics_str = self.post_manager.parse_topics(detail.get("topics", []))
            top_comments = detail.get("top_comments", [])

            if in_window:
                new_daily_no = self.post_manager.get_next_daily_no(window_no)
                self.post_manager.save_post(
                    link_id, new_daily_no, window_start, window_no, detail,
                    content_text, saved_images, topics_str, source="feed",
                    top_comments=top_comments
                )
            else:
                post_window_start, post_window_end = get_window_for_timestamp(real_create_at)
                post_window_no = get_window_no_from_start(post_window_start)
                self.post_manager.save_post(
                    link_id, None, post_window_start, post_window_no, detail,
                    content_text, saved_images, topics_str, source="feed",
                    top_comments=top_comments
                )

            had_analysis = self.llm_analyzer.db.has_analysis(link_id)
            if had_analysis:
                self.llm_analyzer.db.delete_by_link_id(link_id)
                image_descriptions = self.image_analyzer.db.get_descriptions_for_post(link_id)
                post = {
                    "link_id": link_id,
                    "daily_no": new_daily_no if in_window else None,
                    "title": detail.get("title", "(无标题)"),
                    "username": detail.get("username", ""),
                    "userid": detail.get("userid", 0),
                    "create_at": real_create_at,
                    "create_at_str": ts_to_bj_str(real_create_at) if real_create_at else "未知",
                    "content": content_text,
                    "image_paths": saved_images,
                    "image_descriptions": image_descriptions,
                }
                target_ws = window_start if in_window else post_window_start
                await self.llm_analyzer.analyze_posts(target_ws, [post])
                reanalyzed_count += 1
            else:
                skipped_count += 1

            success_count += 1
            if idx < len(existing_posts) - 1:
                await asyncio.sleep(self.content_fetch_interval_seconds)

        logger.info(f"重置完成: 成功 {success_count}/{len(existing_posts)}, 重新分析 {reanalyzed_count} 条")

    @filter.command("今日帖子")
    async def cmd_today_posts(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) >= 2:
            window_no = parts[1].strip()
            if not (len(window_no) == 8 and window_no.isdigit()):
                yield event.plain_result("❌ 窗口编号格式错误，应为 YYYYMMDD")
                return
        else:
            window_no = get_current_window_no()

        try:
            window_start, window_end = get_window_by_no(window_no)
        except Exception as e:
            yield event.plain_result(f"❌ 窗口编号错误: {e}")
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no, title, create_at, userid, username, avatar, topics, content, source, top_comment_count "
            "FROM posts WHERE date_str = ? ORDER BY daily_no",
            (window_no,)
        )
        rows = cur.fetchall()
        conn.close()

        if not rows:
            yield event.plain_result(f"📭 窗口 {window_no} 内还没有帖子")
            return

        header = f"📋 帖子列表 (窗口 {window_no}):\n"
        lines = [header]

        for link_id, daily_no, title, create_at, userid, username, avatar, topics, content, source, top_comment_count in rows:
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

            source_icon = "📨" if source == "at" else "📰"

            post_text = (
                "━━━━━━━━━━━━━━\n"
                "{source_icon} 编号: #{daily_no}\n"
                "   ID: {link_id}\n"
                "   标题: {title_display}\n"
                "   作者: {author_display}\n"
                "   时间: {dt_str}\n"
                "   标签: {topics_display}\n"
                "   热评: {top_comment_count}条\n"
                "   内容:\n{content_cleaned}\n"
            ).format(
                source_icon=source_icon,
                daily_no=daily_no,
                link_id=link_id,
                title_display=title_display,
                author_display=author_display,
                dt_str=dt_str,
                topics_display=topics_display,
                top_comment_count=top_comment_count or 0,
                content_cleaned=content_cleaned
            )
            lines.append(post_text)

        yield event.plain_result("\n".join(lines))

    @filter.command("今日")
    async def cmd_today_detail(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 用法: /今日 <帖子编号>")
            return

        daily_no_input = parts[1]
        if "-" in daily_no_input:
            window_no, seq_no = parse_daily_no(daily_no_input)
        else:
            window_no = get_current_window_no()
            seq_no = int(daily_no_input) if daily_no_input.isdigit() else 0
            daily_no_input = format_daily_no(window_no, seq_no)

        if not window_no or seq_no <= 0:
            yield event.plain_result("❌ 帖子编号格式错误")
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT link_id, daily_no, title, create_at, userid, username, avatar, topics, content, image_urls "
            "FROM posts WHERE daily_no = ?",
            (daily_no_input,)
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            yield event.plain_result(f"❌ 没有找到编号为 #{daily_no_input} 的帖子")
            return

        link_id, daily_no, title, create_at, userid, username, avatar, topics, content, image_urls = row
        dt_str = ts_to_bj_str(create_at)
        title_display = title if title else "(无标题)"
        author_display = username if username else f"用户{userid}"
        content_cleaned = clean_html_tags(content) if content else "无内容"

        # 获取评论
        top_comments = self.post_manager.get_top_comments(link_id)
        comments_text = ""
        if top_comments:
            comments_text = "\n\n🔥 热门评论:\n"
            for c in top_comments:
                comments_text += f"  [{c['rank']}] 👤{c['username']} ❤️{c['up']}\n"
                comments_text += f"      {c['text'][:100]}\n"

        topics_display = ""
        if topics:
            try:
                topics_list = json.loads(topics)
                topics_display = " ".join([f"#{t}" for t in topics_list if t])
            except:
                pass

        text_part = (
            "📌 帖子详情 [编号 #{daily_no}]\n"
            "━━━━━━━━━━━━━━\n"
            "ID: {link_id}\n"
            "标题: {title_display}\n"
            "作者: {author_display}\n"
            "时间: {dt_str}\n"
            "标签: {topics_display}\n"
            "━━━━━━━━━━━━━━\n"
            "{content_cleaned}"
            "{comments_text}"
        ).format(
            daily_no=daily_no,
            link_id=link_id,
            title_display=title_display,
            author_display=author_display,
            dt_str=dt_str,
            topics_display=topics_display,
            content_cleaned=content_cleaned,
            comments_text=comments_text
        )

        chain = [Comp.Plain(text_part)]
        if image_urls:
            try:
                images = json.loads(image_urls)
                for img_path in images:
                    p = Path(img_path)
                    if p.exists():
                        chain.append(Comp.Image.fromFileSystem(str(p)))
            except Exception as e:
                logger.error(f"加载图片失败: {e}")

        yield event.chain_result(chain)

    @filter.command("登录")
    async def cmd_login(self, event: AstrMessageEvent):
        yield event.plain_result("⏳ 正在启动二维码登录（有效期120秒），请稍候...")

        args = [
            "src/main.py", "--login-qr",
            "--qr-timeout-seconds", "120", "--qr-poll-interval", "1"
        ]
        result = await self.post_manager.run_command(args)

        if not result["success"]:
            err = result["stderr"][:500] if result["stderr"] else "未知错误"
            yield event.plain_result(f"❌ 登录执行失败：\n{err}")
            return

        qr_path = self.program_path / "qrcode.png"
        if qr_path.exists():
            yield event.image_result(str(qr_path))
            yield event.plain_result("📸 请使用小黑盒APP扫描二维码进行登录。")
        else:
            yield event.plain_result("⚠️ 未生成二维码图片。")

    @filter.command("分析今日帖子")
    async def cmd_analyze_today(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()

        force_reanalyze = False
        window_no = None

        for part in parts[1:]:
            if part == "--force":
                force_reanalyze = True
            elif len(part) == 8 and part.isdigit():
                window_no = part

        if window_no is None:
            window_no = get_current_window_no()

        if force_reanalyze:
            yield event.plain_result(f"🤖 强制重新分析所有帖子（窗口 {window_no}）...")
        else:
            yield event.plain_result(f"🤖 启动 LLM 分析（窗口 {window_no}）...")
        asyncio.create_task(self._run_llm_analysis(window_no=window_no, force_reanalyze=force_reanalyze))

    @filter.command("今日分析")
    async def cmd_today_analysis(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) >= 2:
            window_no = parts[1].strip()
            if not (len(window_no) == 8 and window_no.isdigit()):
                yield event.plain_result("❌ 窗口编号格式错误")
                return
        else:
            window_no = get_current_window_no()

        try:
            report = await self.llm_analyzer.get_report_by_prefix(window_no)

            if not report:
                yield event.plain_result(f"📭 窗口 {window_no} 还没有分析报告")
                return

            lines = [f"📊 帖子 LLM 分析评论 (窗口 {window_no})\n"]
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
        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) >= 2:
            window_no = parts[1].strip()
            if not (len(window_no) == 8 and window_no.isdigit()):
                yield event.plain_result("❌ 窗口编号格式错误")
                return
        else:
            window_no = get_current_window_no()

        window_start, window_end = get_window_by_no(window_no)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT p.daily_no, p.title, p.username, p.userid, p.avatar,
                   p.create_at, p.content, p.image_urls, l.comment
            FROM posts p
            LEFT JOIN llm_analyses l ON p.link_id = l.link_id
            WHERE p.date_str = ? ORDER BY p.daily_no
        """, (window_no,))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            yield event.plain_result(f"📭 窗口 {window_no} 没有帖子数据")
            return

        posts = []
        for row in rows:
            posts.append({
                "daily_no": row[0], "title": row[1], "username": row[2],
                "userid": row[3], "avatar": row[4],
                "create_at_str": ts_to_bj_str(row[5]) if row[5] else "未知",
                "content": row[6], "image_paths": row[7],
                "comment": row[8] or "暂无评论",
            })

        ai_summary, summary_model, summary_tokens = await generate_ai_summary(
            self.context, posts, window_no, self.llm_provider_id
        )

        issue_no = self.report_generator.calculate_issue_no(window_no)
        report_date = datetime.fromtimestamp(window_end, tz=timezone(timedelta(hours=8))).strftime("%Y年%m月%d日")
        total_comments = len(posts)

        total_input = summary_tokens.get("prompt_tokens", 0) if summary_tokens else 0
        cache_hit = summary_tokens.get("prompt_cache_hit_tokens", 0) if summary_tokens else 0
        completion = summary_tokens.get("completion_tokens", 0) if summary_tokens else 0

        tokens_str = f"{total_input}/{cache_hit}/{completion}" if total_input > 0 or completion > 0 else "--"
        generation_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

        html_content = self.report_generator.generate_evening_report(
            posts=posts, issue_no=issue_no, report_date=report_date,
            community_name="庭院社区", theme="default",
            ai_summary=ai_summary,
            total_comments=total_comments,
            generation_time=generation_time,
            tokens_used=tokens_str,
            model_used=summary_model if summary_model else "--",
        )

        html_path = self.report_generator.save_report(html_content, window_no=window_no)
        yield event.plain_result(f"✅ 晚报 HTML 已保存\n📄 {html_path}")

        if self.evening_report_format == "html":
            yield event.plain_result(f"📎 文件已保存至: {html_path}")
        else:
            yield event.plain_result("🎨 正在渲染 PNG 图片...")
            image_url = await self._render_evening_report_image(html_content)
            if image_url:
                if image_url.startswith("base64://"):
                    img_data = base64.b64decode(image_url[9:])
                    tmp_path = self.report_generator.save_image(img_data, window_no=window_no)
                    yield event.image_result(tmp_path)
                else:
                    yield event.image_result(image_url)
            else:
                yield event.plain_result("❌ 图片渲染失败，但 HTML 已保存")

    @filter.command("调整窗口")
    async def cmd_adjust_window(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 3:
            yield event.plain_result(
                "❌ 用法: /调整窗口 <帖子序号> <目标窗口编号>\n"
                "例如: /调整窗口 4 20260623"
            )
            return

        try:
            post_seq = int(parts[1])
        except ValueError:
            yield event.plain_result("❌ 帖子序号必须是数字")
            return

        target_window_no = parts[2].strip()
        if not (len(target_window_no) == 8 and target_window_no.isdigit()):
            yield event.plain_result("❌ 目标窗口编号格式错误")
            return

        source_window_no = get_current_window_no()
        source_posts = self.post_manager.get_posts_by_window_no(source_window_no)
        if not source_posts:
            yield event.plain_result(f"📭 当前窗口 {source_window_no} 内没有帖子")
            return

        if post_seq < 1 or post_seq > len(source_posts):
            yield event.plain_result(f"❌ 序号 {post_seq} 超出范围")
            return

        link_id, old_daily_no = source_posts[post_seq - 1]

        try:
            result_msg = await self._adjust_post_window(link_id, old_daily_no, source_window_no, target_window_no)
            yield event.plain_result(result_msg)
        except Exception as e:
            logger.error(f"调整窗口失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 调整失败: {str(e)}")

    async def _adjust_post_window(self, link_id: int, old_daily_no: str, 
                                   source_window_no: str, target_window_no: str) -> str:
        """执行帖子窗口调整（重构版：使用 link_id 硬绑定级联同步）"""

        target_window_start, target_window_end = get_window_by_no(target_window_no)
        full_post = self.post_manager._get_full_post(link_id)
        if not full_post:
            return f"❌ 无法获取帖子 ID={link_id} 的完整数据"

        detail = full_post["detail"]
        content_text = full_post["content"]
        saved_images = full_post["images"]
        topics_str = full_post["topics"]

        new_daily_no = self.post_manager.get_next_daily_no(target_window_no)

        # 保存到目标窗口（自动级联同步 llm_analyses）
        self.post_manager.save_post(
            link_id, new_daily_no, target_window_start, target_window_no,
            detail, content_text, saved_images, topics_str, source="feed"
        )

        # 删除源窗口中的旧记录
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("DELETE FROM posts WHERE link_id = ? AND date_str = ?", (link_id, source_window_no))
        conn.commit()
        conn.close()

        # 重新编号源窗口中剩余的帖子（自动级联同步）
        renumbered = self.post_manager.renumber_window_posts(source_window_no)

        logger.info(f"✅ 帖子调整完成: #{old_daily_no} -> #{new_daily_no}, link_id={link_id}")

        msg_lines = [
            "✅ 调整成功！",
            f"📌 帖子 ID: {link_id}",
            f"📋 原编号: #{old_daily_no} (窗口 {source_window_no})",
            f"📋 新编号: #{new_daily_no} (窗口 {target_window_no})",
            f"🕐 新窗口时间: {ts_to_bj_str(target_window_start)} ~ {ts_to_bj_str(target_window_end)}",
        ]
        if renumbered > 0:
            msg_lines.append(f"🔄 源窗口 {source_window_no} 已重新编号 {renumbered} 个帖子")

        return "\n".join(msg_lines)

    @filter.command("调整顺序")
    async def cmd_adjust_order(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 3:
            yield event.plain_result(
                "❌ 用法: /调整顺序 <序号1> <序号2> [窗口编号]\n"
                "例如: /调整顺序 1 3"
            )
            return

        try:
            seq1 = int(parts[1])
            seq2 = int(parts[2])
        except ValueError:
            yield event.plain_result("❌ 序号必须是数字")
            return

        if len(parts) >= 4:
            window_no = parts[3].strip()
            if not (len(window_no) == 8 and window_no.isdigit()):
                yield event.plain_result("❌ 窗口编号格式错误")
                return
        else:
            window_no = get_current_window_no()

        success, result_msg = self.post_manager.swap_daily_no(window_no, seq1, seq2)
        yield event.plain_result(result_msg)

    @filter.command("重置顺序")
    async def cmd_reset_order(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) >= 2:
            window_no = parts[1].strip()
            if not (len(window_no) == 8 and window_no.isdigit()):
                yield event.plain_result("❌ 窗口编号格式错误")
                return
        else:
            window_no = get_current_window_no()

        yield event.plain_result(f"🔄 正在重置窗口 {window_no} 的帖子顺序...")
        renumbered, result_msg = self.post_manager.reset_daily_order(window_no)
        yield event.plain_result(result_msg)

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载时清理任务"""
        self.at_fetcher.stop()
        for task in self._tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info(f"已取消任务: {task.get_name()}")