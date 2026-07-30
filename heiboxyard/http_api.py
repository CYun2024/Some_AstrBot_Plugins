# heiboxyard/http_api.py
import asyncio
import json
import uuid
from datetime import datetime
from aiohttp import web
from astrbot.api import logger
from .utils import get_current_window_no, get_window_by_no, format_daily_no, parse_daily_no

class HeiboxYardAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.app = web.Application()
        self._setup_routes()
        self.runner = None
        self.site = None

        # 任务状态管理
        self.task_status = {}          # task_id -> dict
        self._task_lock = asyncio.Lock()

    def _create_task(self, name: str) -> str:
        """创建任务并返回 task_id"""
        task_id = str(uuid.uuid4())[:8]
        self.task_status[task_id] = {
            "name": name,
            "status": "running",
            "message": "任务已启动",
            "detail": "",
            "progress": "",
            "started_at": datetime.now().isoformat(),
            "finished_at": None
        }
        return task_id

    async def _update_task(self, task_id: str, status: str = None, message: str = None,
                           detail: str = None, progress: str = None):
        """更新任务状态（不结束）"""
        async with self._task_lock:
            if task_id not in self.task_status:
                return
            if status is not None:
                self.task_status[task_id]["status"] = status
            if message is not None:
                self.task_status[task_id]["message"] = message
            if detail is not None:
                self.task_status[task_id]["detail"] = detail
            if progress is not None:
                self.task_status[task_id]["progress"] = progress

    async def _finish_task(self, task_id: str, success: bool, message: str,
                           detail: str = "", progress: str = ""):
        """标记任务完成（成功或失败）"""
        async with self._task_lock:
            if task_id in self.task_status:
                self.task_status[task_id]["status"] = "done" if success else "failed"
                self.task_status[task_id]["message"] = message
                self.task_status[task_id]["detail"] = detail
                self.task_status[task_id]["progress"] = progress
                self.task_status[task_id]["finished_at"] = datetime.now().isoformat()

    # ---------- 辅助方法 ----------
    def _get_post_count_in_window(self, window_no: str) -> int:
        """获取指定窗口的帖子总数"""
        conn = self.plugin.post_manager._get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM posts WHERE date_str = ?", (window_no,))
        count = cur.fetchone()[0]
        conn.close()
        return count

    def _get_analysis_count_in_window(self, window_no: str) -> int:
        """获取指定窗口已有AI评论的帖子数（comment非空）"""
        conn = self.plugin.post_manager._get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM llm_analyses
            WHERE daily_no LIKE ? || '-_%' AND comment IS NOT NULL AND comment != ''
        """, (window_no,))
        count = cur.fetchone()[0]
        conn.close()
        return count

    def _setup_routes(self):
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/windows', self.handle_windows)
        self.app.router.add_post('/api/post/{link_id}/reorder', self.handle_reorder)
        self.app.router.add_post('/api/post/{link_id}/move-window', self.handle_move_window)
        self.app.router.add_post('/api/post/{link_id}/comment', self.handle_update_comment)
        self.app.router.add_post('/api/post/{link_id}/analyze', self.handle_analyze_post)
        self.app.router.add_post('/api/fetch-feed', self.handle_fetch_feed)
        self.app.router.add_post('/api/fetch-at', self.handle_fetch_at)
        self.app.router.add_post('/api/generate-report', self.handle_generate_report)
        self.app.router.add_post('/api/reset-order', self.handle_reset_order)
        self.app.router.add_post('/api/analyze-window', self.handle_analyze_window)
        self.app.router.add_post('/api/post/{link_id}/delete-analysis', self.handle_delete_analysis)
        self.app.router.add_post('/api/summary/generate', self.handle_summary_generate)
        self.app.router.add_get('/api/task/{task_id}', self.handle_task_status)

    async def start(self, host='127.0.0.1', port=5001):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        logger.info(f"HeiboxYard HTTP API 已启动: http://{host}:{port}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("HeiboxYard HTTP API 已停止")

    # ---------- 任务状态查询 ----------
    async def handle_task_status(self, request):
        task_id = request.match_info.get('task_id')
        if task_id not in self.task_status:
            return web.json_response({'success': False, 'error': '任务不存在'}, status=404)
        return web.json_response({'success': True, 'task': self.task_status[task_id]})

    # ---------- 原有处理函数 ----------
    async def handle_stats(self, request):
        pm = self.plugin.post_manager
        conn = pm._get_db_conn()
        cur = conn.cursor()
        current_window = get_current_window_no()
        cur.execute("SELECT COUNT(*) FROM posts WHERE date_str = ?", (current_window,))
        today_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM posts")
        total_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE daily_no LIKE ? || '-_%'", (current_window,))
        today_analyzed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE comment IS NOT NULL AND comment != ''")
        total_analyzed = cur.fetchone()[0]
        conn.close()
        return web.json_response({
            'today_count': today_count,
            'total_count': total_count,
            'today_analyzed': today_analyzed,
            'total_analyzed': total_analyzed,
            'current_window': current_window,
        })

    async def handle_windows(self, request):
        pm = self.plugin.post_manager
        conn = pm._get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT date_str, COUNT(*) as cnt,
                   (SELECT COUNT(*) FROM llm_analyses l
                    JOIN posts p2 ON l.link_id = p2.link_id
                    WHERE p2.date_str = posts.date_str AND l.comment IS NOT NULL AND l.comment != '') as analyzed
            FROM posts
            GROUP BY date_str
            ORDER BY date_str DESC
        """)
        windows = [{'window_no': row[0], 'post_count': row[1], 'analyzed_count': row[2] or 0} for row in cur.fetchall()]
        conn.close()
        return web.json_response({'success': True, 'windows': windows})

    async def handle_reorder(self, request):
        data = await request.json()
        link_id = int(request.match_info['link_id'])
        target_link_id = data.get('target_link_id')
        if not target_link_id:
            return web.json_response({'success': False, 'error': '缺少目标帖子ID'}, status=400)
        success, msg = self.plugin.post_manager.swap_by_link_id(link_id, target_link_id)
        return web.json_response({'success': success, 'message': msg})

    async def handle_move_window(self, request):
        data = await request.json()
        link_id = int(request.match_info['link_id'])
        target_window_no = data.get('target_window_no')
        if not target_window_no or len(target_window_no) != 8:
            return web.json_response({'success': False, 'error': '窗口编号格式错误'}, status=400)
        success, msg = self.plugin.post_manager.move_to_window(link_id, target_window_no)
        return web.json_response({'success': success, 'message': msg})

    async def handle_delete_analysis(self, request):
        link_id = int(request.match_info['link_id'])
        try:
            self.plugin.llm_analyzer.db.delete_by_link_id(link_id)
            return web.json_response({'success': True, 'message': 'AI 评论已删除'})
        except Exception as e:
            logger.error(f"删除分析记录失败 link_id={link_id}: {e}")
            return web.json_response({'success': False, 'error': str(e)}, status=500)

    async def handle_update_comment(self, request):
        data = await request.json()
        link_id = int(request.match_info['link_id'])
        comment = data.get('comment', '')
        pm = self.plugin.post_manager
        conn = pm._get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE link_id = ?", (link_id,))
        exists = cur.fetchone()[0] > 0
        conn.close()
        if exists:
            self.plugin.llm_analyzer.db.update_by_link_id(link_id, new_comment=comment)
        else:
            conn = pm._get_db_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM posts WHERE link_id = ?", (link_id,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return web.json_response({'success': False, 'error': '帖子不存在'}, status=404)
            cols = [desc[0] for desc in cur.description]
            post_dict = dict(zip(cols, row))
            conn.close()
            from .utils import ts_to_bj_str
            post = {
                'link_id': post_dict['link_id'],
                'daily_no': post_dict['daily_no'],
                'title': post_dict['title'] or '(无标题)',
                'username': post_dict['username'] or '未知用户',
                'userid': post_dict['userid'] or 0,
                'create_at': post_dict['create_at'],
                'create_at_str': ts_to_bj_str(post_dict['create_at']) if post_dict['create_at'] else '未知',
                'content': post_dict['content'] or '',
                'image_paths': json.loads(post_dict['image_urls']) if post_dict['image_urls'] else [],
                'window_start': post_dict['window_start'],
            }
            analysis = {
                'daily_no': post_dict['daily_no'],
                'comment': comment,
                'sentiment': 'neutral',
            }
            self.plugin.llm_analyzer.db.save_analyses(
                post_dict['window_start'],
                [post],
                [analysis],
                json.dumps(analysis, ensure_ascii=False),
                'manual'
            )
        return web.json_response({'success': True, 'message': '评论已更新'})

    async def handle_analyze_post(self, request):
        link_id = int(request.match_info['link_id'])
        pm = self.plugin.post_manager
        conn = pm._get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM posts WHERE link_id = ?", (link_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return web.json_response({'success': False, 'error': '帖子不存在'}, status=404)
        cols = [desc[0] for desc in cur.description]
        post_dict = dict(zip(cols, row))
        conn.close()
        from .utils import ts_to_bj_str
        post = {
            'link_id': post_dict['link_id'],
            'daily_no': post_dict['daily_no'],
            'title': post_dict['title'] or '(无标题)',
            'username': post_dict['username'] or '未知用户',
            'userid': post_dict['userid'] or 0,
            'create_at': post_dict['create_at'],
            'create_at_str': ts_to_bj_str(post_dict['create_at']) if post_dict['create_at'] else '未知',
            'content': post_dict['content'] or '',
            'image_paths': json.loads(post_dict['image_urls']) if post_dict['image_urls'] else [],
            'window_start': post_dict['window_start'],
        }
        image_descs = self.plugin.image_analyzer.db.get_descriptions_for_post(link_id)
        post['image_descriptions'] = image_descs
        all_comments = pm.get_top_comments(link_id)
        hot_comments = [c for c in all_comments if c.get('up', 0) >= self.plugin.hot_comment_threshold]
        post['hot_comments'] = hot_comments
        analysis, tokens = await self.plugin.llm_analyzer.analyze_single_post(post)
        if analysis and analysis.get('comment'):
            self.plugin.llm_analyzer.db.save_analyses(
                post['window_start'], [post], [analysis],
                json.dumps(analysis), analysis.get('model_used', 'unknown')
            )
            return web.json_response({'success': True, 'comment': analysis['comment']})
        else:
            return web.json_response({'success': False, 'error': '分析失败'}, status=500)

    # ---------- 异步任务包装 ----------

    async def _run_fetch_feed(self, task_id):
        current_window = get_current_window_no()
        before = self._get_post_count_in_window(current_window)
        await self._update_task(task_id, message="正在拉取 Feed...", detail="正在获取社区最新帖子", progress="")
        try:
            await self.plugin._fetch_and_process_feed()
            after = self._get_post_count_in_window(current_window)
            added = after - before
            if added == 0:
                msg = f"Feed拉取完成，当前窗口 {current_window} 共有 {after} 个帖子，没有新增帖子"
                detail = "没有新帖子"
            else:
                msg = f"Feed拉取完成，当前窗口 {current_window} 共有 {after} 个帖子（新增 {added} 个）"
                detail = f"新增 {added} 个帖子"
            await self._finish_task(task_id, True, msg, detail)
        except Exception as e:
            logger.error(f"Feed 拉取任务失败: {e}", exc_info=True)
            await self._finish_task(task_id, False, f"Feed 拉取失败: {str(e)}", "")

    async def _run_fetch_at(self, task_id):
        current_window = get_current_window_no()
        before = self._get_post_count_in_window(current_window)
        await self._update_task(task_id, message="正在拉取 @消息...", detail="正在获取 @提及 的帖子", progress="")
        try:
            count = await self.plugin.at_fetcher.manual_fetch()
            after = self._get_post_count_in_window(current_window)
            added = after - before
            if added == 0:
                msg = f"@消息拉取完成，处理了 {count} 条消息，当前窗口 {current_window} 没有新增帖子"
                detail = "没有新帖子"
            else:
                msg = f"@消息拉取完成，处理了 {count} 条消息，当前窗口 {current_window} 新增 {added} 个帖子"
                detail = f"新增 {added} 个帖子"
            await self._finish_task(task_id, True, msg, detail)
        except Exception as e:
            logger.error(f"@消息拉取任务失败: {e}", exc_info=True)
            await self._finish_task(task_id, False, f"@消息拉取失败: {str(e)}", "")

    async def _run_generate_report(self, task_id, window_no):
        await self._update_task(task_id, message=f"正在生成晚报 (窗口 {window_no})...", detail="正在生成 HTML 和图片...", progress="")
        try:
            await self.plugin._generate_and_save_evening_report(window_no=window_no, send=False)
            await self._finish_task(task_id, True, f"晚报生成完成 (窗口 {window_no})", "已保存到 reports 目录")
        except Exception as e:
            logger.error(f"生成晚报任务失败: {e}", exc_info=True)
            await self._finish_task(task_id, False, f"生成晚报失败: {str(e)}", "")

    async def _run_analyze_window(self, task_id, window_no, force_reanalyze=False):
        total_posts = self._get_post_count_in_window(window_no)
        before_analyzed = self._get_analysis_count_in_window(window_no)
        await self._update_task(task_id, message=f"正在 AI 分析 (窗口 {window_no})...",
                               detail=f"共 {total_posts} 个帖子，已分析 {before_analyzed} 个", progress="")
        try:
            tokens = await self.plugin._run_llm_analysis(window_no=window_no, force_reanalyze=force_reanalyze)
            after_analyzed = self._get_analysis_count_in_window(window_no)
            added = after_analyzed - before_analyzed
            if added == 0:
                if before_analyzed == total_posts:
                    msg = f"AI分析完成，窗口 {window_no} 全部 {total_posts} 个帖子均已分析"
                else:
                    msg = f"AI分析完成，窗口 {window_no} 共 {total_posts} 个帖子，已分析 {after_analyzed} 个（无新增）"
                detail = "无需新增分析"
            else:
                msg = f"AI分析完成，窗口 {window_no} 共 {total_posts} 个帖子，已分析 {after_analyzed} 个（新增 {added} 个）"
                detail = f"新增 {added} 条评论"
            # 添加 token 信息（如果有）
            if tokens and tokens.get('total_tokens'):
                detail += f"，消耗 tokens: {tokens['total_tokens']}"
            await self._finish_task(task_id, True, msg, detail)
        except Exception as e:
            logger.error(f"AI分析任务失败: {e}", exc_info=True)
            await self._finish_task(task_id, False, f"AI分析失败: {str(e)}", "")

    async def _run_summary_generate(self, task_id, window_no):
        await self._update_task(task_id, message=f"正在重新生成总评 (窗口 {window_no})...", detail="", progress="")
        try:
            await self.plugin._force_regenerate_summary(window_no)
            await self._finish_task(task_id, True, f"总评重新生成完成 (窗口 {window_no})", "已更新总评")
        except Exception as e:
            logger.error(f"总评重新生成任务失败: {e}", exc_info=True)
            await self._finish_task(task_id, False, f"总评重新生成失败: {str(e)}", "")

    # ---------- API 处理函数 ----------

    async def handle_fetch_feed(self, request):
        task_id = self._create_task("拉取Feed")
        asyncio.create_task(self._run_fetch_feed(task_id))
        return web.json_response({'success': True, 'task_id': task_id, 'message': 'Feed拉取任务已启动'})

    async def handle_fetch_at(self, request):
        data = await request.json() or {}
        window_no = data.get('window_no')
        task_id = self._create_task("拉取@消息")
        asyncio.create_task(self._run_fetch_at(task_id))
        return web.json_response({'success': True, 'task_id': task_id, 'message': '@消息拉取任务已启动'})

    async def handle_generate_report(self, request):
        data = await request.json() or {}
        window_no = data.get('window_no') or get_current_window_no()
        task_id = self._create_task(f"生成晚报 {window_no}")
        asyncio.create_task(self._run_generate_report(task_id, window_no))
        return web.json_response({'success': True, 'task_id': task_id, 'message': f'晚报生成任务已启动 (窗口 {window_no})'})

    async def handle_reset_order(self, request):
        data = await request.json()
        window_no = data.get('window_no')
        if not window_no:
            return web.json_response({'success': False, 'error': '缺少窗口编号'}, status=400)
        renumbered, msg = self.plugin.post_manager.reset_daily_order(window_no)
        return web.json_response({'success': True, 'message': msg})

    async def handle_analyze_window(self, request):
        data = await request.json() or {}
        window_no = data.get('window_no') or get_current_window_no()
        force_reanalyze = data.get('force_reanalyze', False)
        task_id = self._create_task(f"AI分析 {window_no}")
        asyncio.create_task(self._run_analyze_window(task_id, window_no, force_reanalyze))
        return web.json_response({
            'success': True,
            'task_id': task_id,
            'message': f'AI 分析任务已启动 (窗口 {window_no})'
        })

    async def handle_summary_generate(self, request):
        data = await request.json() or {}
        window_no = data.get('window_no') or get_current_window_no()
        task_id = self._create_task(f"重新生成总评 {window_no}")
        asyncio.create_task(self._run_summary_generate(task_id, window_no))
        return web.json_response({'success': True, 'task_id': task_id, 'message': f'总评重新生成任务已启动 (窗口 {window_no})'})