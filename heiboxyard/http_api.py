# heiboxyard/http_api.py
import asyncio
import json
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

    # ---------- 处理函数 ----------

    async def handle_stats(self, request):
        """返回统计数据"""
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
        """返回所有窗口及帖子数"""
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
        """交换两个帖子的 daily_no"""
        data = await request.json()
        link_id = int(request.match_info['link_id'])
        target_link_id = data.get('target_link_id')
        if not target_link_id:
            return web.json_response({'success': False, 'error': '缺少目标帖子ID'}, status=400)
        success, msg = self.plugin.post_manager.swap_by_link_id(link_id, target_link_id)
        return web.json_response({'success': success, 'message': msg})

    async def handle_move_window(self, request):
        """移动帖子到另一个窗口"""
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
        comment = data.get('comment', '')   # 可为空

        pm = self.plugin.post_manager
        # 检查记录是否存在
        conn = pm._get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE link_id = ?", (link_id,))
        exists = cur.fetchone()[0] > 0
        conn.close()

        if exists:
            self.plugin.llm_analyzer.db.update_by_link_id(link_id, new_comment=comment)
        else:
            # 插入新记录（含空评论）
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
        """触发单个帖子的 AI 分析"""
        link_id = int(request.match_info['link_id'])
        # 获取帖子数据
        pm = self.plugin.post_manager
        conn = pm._get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM posts WHERE link_id = ?", (link_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return web.json_response({'success': False, 'error': '帖子不存在'}, status=404)
        # 构造帖子字典
        # 重新获取连接并查询
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
        # 构造分析所需格式
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
        # 获取图片描述
        image_descs = self.plugin.image_analyzer.db.get_descriptions_for_post(link_id)
        post['image_descriptions'] = image_descs
        # 获取热评
        all_comments = pm.get_top_comments(link_id)
        hot_comments = [c for c in all_comments if c.get('up', 0) >= self.plugin.hot_comment_threshold]
        post['hot_comments'] = hot_comments

        # 调用 llm_analyzer 的单帖分析
        analysis, tokens = await self.plugin.llm_analyzer.analyze_single_post(post)
        if analysis and analysis.get('comment'):
            # 保存
            self.plugin.llm_analyzer.db.save_analyses(
                post['window_start'], [post], [analysis],
                json.dumps(analysis), analysis.get('model_used', 'unknown')
            )
            return web.json_response({'success': True, 'comment': analysis['comment']})
        else:
            return web.json_response({'success': False, 'error': '分析失败'}, status=500)

    async def handle_fetch_feed(self, request):
        """触发 Feed 拉取（异步执行）"""
        asyncio.create_task(self.plugin._fetch_and_process_feed())
        return web.json_response({'success': True, 'message': 'Feed 拉取任务已启动'})

    async def handle_fetch_at(self, request):
        """触发 @消息拉取（异步执行）"""
        data = await request.json() or {}
        window_no = data.get('window_no')
        asyncio.create_task(self.plugin.at_fetcher.manual_fetch())
        return web.json_response({'success': True, 'message': '@消息拉取任务已启动'})

    async def handle_generate_report(self, request):
        """生成晚报（异步执行）"""
        data = await request.json() or {}
        window_no = data.get('window_no') or get_current_window_no()
        asyncio.create_task(self.plugin._generate_and_save_evening_report(window_no=window_no, send=False))
        return web.json_response({'success': True, 'message': f'晚报生成任务已启动 (窗口 {window_no})'})

    async def handle_reset_order(self, request):
        """重置窗口帖子顺序"""
        data = await request.json()
        window_no = data.get('window_no')
        if not window_no:
            return web.json_response({'success': False, 'error': '缺少窗口编号'}, status=400)
        renumbered, msg = self.plugin.post_manager.reset_daily_order(window_no)
        return web.json_response({'success': True, 'message': msg})

    # ========== 新增：仅分析不生成报告 ==========
    async def handle_analyze_window(self, request):
        """仅执行 AI 分析（逐帖评论 + 总评），不生成晚报 HTML"""
        data = await request.json() or {}
        window_no = data.get('window_no') or get_current_window_no()
        asyncio.create_task(self.plugin._run_llm_analysis(window_no=window_no, force_reanalyze=False))
        return web.json_response({
            'success': True,
            'message': f'AI 分析任务已启动 (窗口 {window_no})'
        })

    async def handle_summary_generate(self, request):
        data = await request.json() or {}
        window_no = data.get('window_no') or get_current_window_no()
        # 改为强制重新生成
        asyncio.create_task(self.plugin._force_regenerate_summary(window_no))
        return web.json_response({'success': True, 'message': f'总评重新生成任务已启动 (窗口 {window_no})'})

def ts_to_bj_str(ts):
    from datetime import datetime, timezone, timedelta
    if not ts:
        return "未知"
    dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")