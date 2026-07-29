# -*- coding: utf-8 -*-
"""
heiboxyard Web 管理端 — Flask Blueprint
挂载到 astrbot_reports_server 上，提供可视化帖子管理界面

路由前缀: /yard/admin
"""
import json
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, abort, flash, redirect, url_for
)

# 导入 requests 用于代理 API
try:
    import requests
except ImportError:
    requests = None
    print("⚠️ requests 库未安装，API 代理将无法工作")

# ========== 配置 ==========
DEFAULT_DB_PATH = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/posts.db"
DEFAULT_REPORTS_DIR = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/reports"
PLUGIN_API_BASE = os.environ.get('HEIBOXYARD_API_URL', 'http://127.0.0.1:5001')

# 创建 Blueprint
yard_bp = Blueprint(
    'yard_admin',
    __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/yard/admin/static',
    url_prefix='/yard/admin'
)

def get_db_path():
    return current_app.config.get('HEIBOXYARD_DB_PATH', DEFAULT_DB_PATH)

def get_reports_dir():
    return current_app.config.get('HEIBOXYARD_REPORTS_DIR', DEFAULT_REPORTS_DIR)

def get_db():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn

def ts_to_bj_str(timestamp):
    if not timestamp:
        return "未知"
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def get_window_by_no(window_no):
    dt = datetime.strptime(window_no, "%Y%m%d")
    window_end = dt.replace(hour=22, minute=0, second=0, microsecond=0,
                           tzinfo=timezone(timedelta(hours=8)))
    window_start = window_end - timedelta(days=1)
    return (
        int(window_start.astimezone(timezone.utc).timestamp()),
        int(window_end.astimezone(timezone.utc).timestamp())
    )

def get_current_window_no():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    dt = datetime.fromtimestamp(now_ts, tz=timezone(timedelta(hours=8)))
    day_22 = dt.replace(hour=22, minute=0, second=0, microsecond=0)
    if dt.hour >= 22:
        window_end = day_22 + timedelta(days=1)
    else:
        window_end = day_22
    return window_end.strftime("%Y%m%d")

def parse_daily_no(daily_no_str):
    import re
    match = re.match(r'^(\d{8})-(\d+)$', daily_no_str)
    if match:
        return match.group(1), int(match.group(2))
    return "", 0

def format_daily_no(window_no, seq_no):
    return f"{window_no}-{seq_no:02d}"

# ========== 页面路由 ==========

@yard_bp.route('/')
def dashboard():
    conn = get_db()
    cur = conn.cursor()
    current_window_no = get_current_window_no()
    window_start, window_end = get_window_by_no(current_window_no)
    cur.execute("SELECT COUNT(*) FROM posts WHERE date_str = ?", (current_window_no,))
    today_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM posts")
    total_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE daily_no LIKE ? || '-_%'", (current_window_no,))
    today_analyzed = cur.fetchone()[0]
    cur.execute("SELECT DISTINCT date_str FROM posts ORDER BY date_str DESC LIMIT 10")
    recent_windows = [row[0] for row in cur.fetchall()]
    cur.execute("""
        SELECT p.link_id, p.daily_no, p.title, p.username, p.create_at,
               p.top_comment_count, l.comment as ai_comment
        FROM posts p
        LEFT JOIN llm_analyses l ON p.link_id = l.link_id
        WHERE p.date_str = ?
        ORDER BY p.daily_no
    """, (current_window_no,))
    today_posts = [dict(row) for row in cur.fetchall()]
    conn.close()
    return render_template('dashboard.html',
        current_window_no=current_window_no,
        today_count=today_count,
        total_count=total_count,
        today_analyzed=today_analyzed,
        recent_windows=recent_windows,
        today_posts=today_posts,
        ts_to_bj_str=ts_to_bj_str,
        nav_window=current_window_no
    )

@yard_bp.route('/posts')
def posts_list():
    conn = get_db()
    cur = conn.cursor()
    window_no = request.args.get('window', get_current_window_no())
    search = request.args.get('search', '').strip()
    source = request.args.get('source', '')
    has_analysis = request.args.get('has_analysis', '')
    page = request.args.get('page', 1, type=int)
    per_page = 20

    cur.execute("SELECT DISTINCT date_str FROM posts ORDER BY date_str DESC")
    all_windows = [row[0] for row in cur.fetchall()]
    # 确保当前查询的窗口在列表中（即使没有帖子）
    if window_no not in all_windows:
        all_windows.append(window_no)
        all_windows.sort(reverse=True)

    where_clauses = ["p.date_str = ?"]
    params = [window_no]
    if search:
        where_clauses.append("(p.title LIKE ? OR p.username LIKE ? OR p.content LIKE ?)")
        like_str = f"%{search}%"
        params.extend([like_str, like_str, like_str])
    if source:
        where_clauses.append("p.source = ?")
        params.append(source)
    if has_analysis == 'yes':
        where_clauses.append("l.comment IS NOT NULL AND l.comment != ''")
    elif has_analysis == 'no':
        where_clauses.append("(l.comment IS NULL OR l.comment = '')")

    where_sql = " AND ".join(where_clauses)
    count_sql = f"""
        SELECT COUNT(*) FROM posts p
        LEFT JOIN llm_analyses l ON p.link_id = l.link_id
        WHERE {where_sql}
    """
    cur.execute(count_sql, params)
    total = cur.fetchone()[0]

    offset = (page - 1) * per_page
    query_sql = f"""
        SELECT p.link_id, p.daily_no, p.title, p.username, p.userid,
               p.create_at, p.source, p.top_comment_count,
               p.image_urls, l.comment, l.model_used
        FROM posts p
        LEFT JOIN llm_analyses l ON p.link_id = l.link_id
        WHERE {where_sql}
        ORDER BY p.daily_no
        LIMIT ? OFFSET ?
    """
    cur.execute(query_sql, params + [per_page, offset])
    posts = [dict(row) for row in cur.fetchall()]

    for post in posts:
        if post.get('image_urls'):
            try:
                post['image_count'] = len(json.loads(post['image_urls']))
            except:
                post['image_count'] = 0
        else:
            post['image_count'] = 0

    conn.close()
    total_pages = (total + per_page - 1) // per_page
    return render_template('posts.html',
        posts=posts,
        window_no=window_no,
        all_windows=all_windows,
        search=search,
        source=source,
        has_analysis=has_analysis,
        page=page,
        total_pages=total_pages,
        total=total,
        ts_to_bj_str=ts_to_bj_str,
        nav_window=window_no,
        current_window_no=get_current_window_no()
    )

@yard_bp.route('/post/<int:link_id>')
def post_detail(link_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.*, l.comment as ai_comment, l.model_used, l.analyzed_at
        FROM posts p
        LEFT JOIN llm_analyses l ON p.link_id = l.link_id
        WHERE p.link_id = ?
    """, (link_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        abort(404)
    post = dict(row)
    image_paths = []
    if post.get('image_urls'):
        try:
            image_paths = json.loads(post['image_urls'])
        except:
            pass
    post['image_paths'] = image_paths
    topics = []
    if post.get('topics'):
        try:
            topics = json.loads(post['topics'])
        except:
            pass
    post['topics'] = topics

    cur.execute("""
        SELECT comment_id, rank, username, user_id, avatar, text, up,
               has_image, images, comment_time
        FROM post_comments
        WHERE link_id = ?
        ORDER BY rank
    """, (link_id,))
    comments = [dict(row) for row in cur.fetchall()]
    for c in comments:
        if c.get('images'):
            try:
                c['images'] = json.loads(c['images'])
            except:
                c['images'] = []
        if c.get('comment_time'):
            c['time_str'] = ts_to_bj_str(c['comment_time'])

    cur.execute("SELECT DISTINCT date_str FROM posts ORDER BY date_str DESC LIMIT 20")
    all_windows = [row[0] for row in cur.fetchall()]

    if post.get('date_str'):
        cur.execute("""
            SELECT link_id, daily_no, title, username
            FROM posts
            WHERE date_str = ? AND link_id != ?
            ORDER BY daily_no
        """, (post['date_str'], link_id))
        same_window_posts = [dict(row) for row in cur.fetchall()]
        nav_window = post['date_str']
    else:
        same_window_posts = []
        nav_window = get_current_window_no()

    conn.close()
    return render_template('post_detail.html',
        post=post,
        comments=comments,
        all_windows=all_windows,
        same_window_posts=same_window_posts,
        ts_to_bj_str=ts_to_bj_str,
        nav_window=nav_window,
        current_window_no=get_current_window_no()
    )

@yard_bp.route('/reports')
def reports_list():
    reports_dir = Path(get_reports_dir())
    if not reports_dir.exists():
        reports_dir.mkdir(parents=True, exist_ok=True)

    report_files = []
    for f in sorted(reports_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix.lower() in ('.html', '.png', '.jpg', '.jpeg'):
            stat = f.stat()
            size = stat.st_size
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"

            report_files.append({
                'name': f.name,
                'size': size_str,
                'mtime': datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                'type': 'html' if f.suffix.lower() == '.html' else 'image',
                'path': str(f)
            })
    return render_template('reports.html',
        reports=report_files,
        nav_window=get_current_window_no(),
        current_window_no=get_current_window_no()
    )

# ========== 总评管理页面 ==========
@yard_bp.route('/summary')
def summary_manage():
    window_no = request.args.get('window', get_current_window_no())
    window_start, _ = get_window_by_no(window_no)
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT comment, model_used, analyzed_at
        FROM llm_analyses
        WHERE daily_no = 'SUMMARY' AND window_start = ?
    """, (window_start,))
    row = cur.fetchone()
    conn.close()
    
    summary = None
    if row:
        summary = {'comment': row[0], 'model': row[1], 'analyzed_at': row[2]}
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date_str FROM posts ORDER BY date_str DESC")
    all_windows = [row[0] for row in cur.fetchall()]
    conn.close()
    if window_no not in all_windows:
        all_windows.append(window_no)
        all_windows.sort(reverse=True)
    
    return render_template('summary.html',
        window_no=window_no,
        summary=summary,
        all_windows=all_windows,
        nav_window=window_no,
        current_window_no=get_current_window_no()
    )

# ========== API 代理路由 ==========
# 注意：所有 /api/* 路由都代理到插件 HTTP API

def proxy_request(method, endpoint, data=None, timeout=30):
    """统一的代理请求函数"""
    if requests is None:
        return jsonify({'success': False, 'error': 'requests 库未安装'}), 500
    url = f"{PLUGIN_API_BASE}{endpoint}"
    try:
        if method == 'GET':
            resp = requests.get(url, timeout=timeout)
        elif method == 'POST':
            # 确保 data 至少是空字典
            if data is None:
                data = {}
            resp = requests.post(url, json=data, timeout=timeout)
        else:
            return jsonify({'success': False, 'error': '不支持的请求方法'}), 400
        # 如果插件 API 返回非 JSON，尝试解析
        try:
            return jsonify(resp.json()), resp.status_code
        except:
            # 如果响应不是 JSON，返回错误
            return jsonify({'success': False, 'error': f'插件 API 返回了非 JSON 响应: {resp.text[:200]}'}), 500
    except requests.exceptions.ConnectionError:
        return jsonify({'success': False, 'error': '无法连接到插件 API，请确保插件已启动'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@yard_bp.route('/api/stats')
def api_stats():
    return proxy_request('GET', '/api/stats')

@yard_bp.route('/api/windows')
def api_windows():
    return proxy_request('GET', '/api/windows')

@yard_bp.route('/api/post/<int:link_id>/reorder', methods=['POST'])
def api_reorder_post(link_id):
    data = request.get_json() or {}
    return proxy_request('POST', f'/api/post/{link_id}/reorder', data)

@yard_bp.route('/api/post/<int:link_id>/move-window', methods=['POST'])
def api_move_window(link_id):
    data = request.get_json() or {}
    return proxy_request('POST', f'/api/post/{link_id}/move-window', data)

@yard_bp.route('/api/post/<int:link_id>/comment', methods=['POST'])
def api_update_comment(link_id):
    data = request.get_json() or {}
    return proxy_request('POST', f'/api/post/{link_id}/comment', data)

@yard_bp.route('/api/post/<int:link_id>/analyze', methods=['POST'])
def api_analyze_post(link_id):
    return proxy_request('POST', f'/api/post/{link_id}/analyze', timeout=120)

@yard_bp.route('/api/fetch-feed', methods=['POST'])
def api_fetch_feed():
    return proxy_request('POST', '/api/fetch-feed')

@yard_bp.route('/api/fetch-at', methods=['POST'])
def api_fetch_at():
    data = request.get_json() or {}
    return proxy_request('POST', '/api/fetch-at', data)

@yard_bp.route('/api/generate-report', methods=['POST'])
def api_generate_report():
    data = request.get_json() or {}
    return proxy_request('POST', '/api/generate-report', data)

@yard_bp.route('/api/reset-order', methods=['POST'])
def api_reset_order():
    data = request.get_json() or {}
    return proxy_request('POST', '/api/reset-order', data)

@yard_bp.route('/api/post/<int:link_id>/delete-analysis', methods=['POST'])
def api_delete_analysis(link_id):
    return proxy_request('POST', f'/api/post/{link_id}/delete-analysis')

@yard_bp.route('/api/analyze-window', methods=['POST'])
def api_analyze_window():
    data = request.get_json() or {}
    return proxy_request('POST', '/api/analyze-window', data)

# ========== 总评相关 API ==========
@yard_bp.route('/api/summary', methods=['GET'])
def api_get_summary():
    """获取指定窗口的总评"""
    window_no = request.args.get('window_no') or get_current_window_no()
    window_start, _ = get_window_by_no(window_no)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT comment, model_used, analyzed_at
        FROM llm_analyses
        WHERE daily_no = 'SUMMARY' AND window_start = ?
    """, (window_start,))
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify({
            'success': True,
            'comment': row[0],
            'model': row[1],
            'analyzed_at': row[2]
        })
    return jsonify({'success': False, 'comment': None})

@yard_bp.route('/api/summary/update', methods=['POST'])
def api_update_summary():
    """更新或插入总评"""
    data = request.get_json() or {}
    window_no = data.get('window_no') or get_current_window_no()
    comment = data.get('comment', '').strip()
    window_start, _ = get_window_by_no(window_no)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE daily_no = 'SUMMARY' AND window_start = ?", (window_start,))
    exists = cur.fetchone()[0] > 0
    if exists:
        cur.execute("""
            UPDATE llm_analyses SET comment = ?, analyzed_at = ?
            WHERE daily_no = 'SUMMARY' AND window_start = ?
        """, (comment, datetime.now(timezone.utc).isoformat(), window_start))
    else:
        cur.execute("""
            INSERT INTO llm_analyses (window_start, daily_no, link_id, title, comment, analyzed_at, model_used)
            VALUES (?, 'SUMMARY', 0, 'AI总评', ?, ?, 'manual')
        """, (window_start, comment, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '总评已更新'})

@yard_bp.route('/api/summary/generate', methods=['POST'])
def api_generate_summary():
    """触发插件重新生成总评（异步）"""
    data = request.get_json() or {}
    return proxy_request('POST', '/api/summary/generate', data)