# -*- coding: utf-8 -*-
"""
heiboxyard Web 管理端 — Flask Blueprint
挂载到 astrbot_reports_server 上，提供可视化帖子管理界面

路由前缀: /yard/admin
"""
import json
import sqlite3
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, abort, flash, redirect, url_for, session, send_file
)

try:
    import requests
except ImportError:
    requests = None
    print("⚠️ requests 库未安装，API 代理将无法工作")

# ========== 配置 ==========
DEFAULT_DB_PATH = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/posts.db"
DEFAULT_REPORTS_DIR = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/reports"
PLUGIN_API_BASE = os.environ.get('HEIBOXYARD_API_URL', 'http://127.0.0.1:5001')

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

# ========== 操作日志 ==========
_log_table_initialized = False

def init_log_table():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_nickname TEXT,
            user_ip TEXT,
            device_id TEXT,
            operation_type TEXT,
            detail TEXT,
            timestamp INTEGER
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_op_timestamp ON operation_log(timestamp DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_op_user ON operation_log(user_nickname)')
    conn.commit()
    conn.close()

def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr

def log_operation(operation_type, detail=None, nickname=None, ip=None, device_id=None):
    global _log_table_initialized
    if not _log_table_initialized:
        init_log_table()
        _log_table_initialized = True

    if nickname is None:
        nickname = session.get('nickname')
        if not nickname:
            req_ip = get_client_ip()
            req_device = session.get('device_id')
            today = datetime.now().strftime('%Y-%m-%d')
            conn = get_db()
            cur = conn.cursor()
            if req_device:
                cur.execute('''
                    SELECT nickname FROM access_whitelist
                    WHERE device_id=? AND date=? AND authorized=1
                    ORDER BY id DESC LIMIT 1
                ''', (req_device, today))
                row = cur.fetchone()
                if row:
                    nickname = row[0]
            if not nickname:
                cur.execute('''
                    SELECT nickname FROM access_whitelist
                    WHERE ip=? AND date=? AND authorized=1
                    ORDER BY id DESC LIMIT 1
                ''', (req_ip, today))
                row = cur.fetchone()
                if row:
                    nickname = row[0]
            conn.close()
        if not nickname:
            nickname = 'unknown'

    if ip is None:
        ip = get_client_ip()
    if device_id is None:
        device_id = session.get('device_id', '')

    if detail is None:
        detail = {}
    try:
        detail_str = json.dumps(detail, ensure_ascii=False)
    except:
        detail_str = str(detail)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO operation_log (user_nickname, user_ip, device_id, operation_type, detail, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (nickname, ip, device_id, operation_type, detail_str, int(time.time()))
    )
    conn.commit()
    conn.close()

# ========== 代理请求 ==========
def proxy_request(method, endpoint, data=None, timeout=30):
    if requests is None:
        return jsonify({'success': False, 'error': 'requests 库未安装'}), 500
    url = f"{PLUGIN_API_BASE}{endpoint}"
    try:
        if method == 'GET':
            resp = requests.get(url, timeout=timeout)
        elif method == 'POST':
            if data is None:
                data = {}
            resp = requests.post(url, json=data, timeout=timeout)
        else:
            return jsonify({'success': False, 'error': '不支持的请求方法'}), 400
        try:
            return resp.json(), resp.status_code
        except:
            return {'success': False, 'error': f'插件 API 返回非 JSON: {resp.text[:200]}'}, 500
    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': '无法连接到插件 API，请确保插件已启动'}, 500
    except Exception as e:
        return {'success': False, 'error': str(e)}, 500

# ========== 页面路由 ==========
@yard_bp.route('/')
def dashboard():
    conn = get_db()
    cur = conn.cursor()
    current_window_no = get_current_window_no()
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

    groups = {}
    for f in reports_dir.iterdir():
        if f.suffix.lower() not in ('.html', '.png', '.jpg', '.jpeg'):
            continue
        basename = f.stem
        if basename not in groups:
            groups[basename] = {'html': None, 'image': None, 'mtime': None}
        ext = f.suffix.lower()
        if ext == '.html':
            groups[basename]['html'] = f
        else:
            groups[basename]['image'] = f
        if groups[basename]['mtime'] is None or f.stat().st_mtime > groups[basename]['mtime']:
            groups[basename]['mtime'] = f.stat().st_mtime

    report_items = []
    for basename, info in groups.items():
        if info['html'] is None and info['image'] is None:
            continue
        if info['html'] and info['image']:
            type_label = '网页+图片'
        elif info['html']:
            type_label = '网页'
        else:
            type_label = '图片'
        size_bytes = 0
        if info['html']:
            size_bytes += info['html'].stat().st_size
        if info['image']:
            size_bytes += info['image'].stat().st_size
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"

        mtime_str = datetime.fromtimestamp(info['mtime']).strftime("%Y-%m-%d %H:%M:%S")

        report_items.append({
            'basename': basename,
            'html_name': info['html'].name if info['html'] else None,
            'image_name': info['image'].name if info['image'] else None,
            'size': size_str,
            'mtime': mtime_str,
            'type': type_label,
            'has_html': bool(info['html']),
            'has_image': bool(info['image'])
        })

    report_items.sort(key=lambda x: x['mtime'], reverse=True)

    return render_template('reports.html',
        reports=report_items,
        nav_window=get_current_window_no(),
        current_window_no=get_current_window_no()
    )

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

@yard_bp.route('/logs')
def logs_page():
    if not session.get('authorized') or session.get('role') != 'admin':
        abort(403)
    return render_template('logs.html',
        nav_window=get_current_window_no(),
        current_window_no=get_current_window_no()
    )

# ========== API 代理路由（增强日志） ==========
@yard_bp.route('/api/stats')
def api_stats():
    return jsonify(proxy_request('GET', '/api/stats')[0]), 200

@yard_bp.route('/api/windows')
def api_windows():
    return jsonify(proxy_request('GET', '/api/windows')[0]), 200

@yard_bp.route('/api/post/<int:link_id>/reorder', methods=['POST'])
def api_reorder_post(link_id):
    data = request.get_json() or {}
    target_link_id = data.get('target_link_id')
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT daily_no FROM posts WHERE link_id = ?", (link_id,))
    row = cur.fetchone()
    old_no = row[0] if row else None
    cur.execute("SELECT daily_no FROM posts WHERE link_id = ?", (target_link_id,))
    row = cur.fetchone()
    target_old_no = row[0] if row else None
    conn.close()

    log_operation('swap_order', {
        'link_id': link_id,
        'old_daily_no': old_no,
        'target_link_id': target_link_id,
        'target_old_daily_no': target_old_no
    })

    resp, status = proxy_request('POST', f'/api/post/{link_id}/reorder', data)
    if not resp.get('success', False):
        log_operation('swap_order_failed', {'link_id': link_id, 'target_link_id': target_link_id, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/post/<int:link_id>/move-window', methods=['POST'])
def api_move_window(link_id):
    data = request.get_json() or {}
    target_window = data.get('target_window_no')
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT date_str, daily_no FROM posts WHERE link_id = ?", (link_id,))
    row = cur.fetchone()
    old_window = row[0] if row else None
    old_daily_no = row[1] if row else None
    
    cur.execute("SELECT COUNT(*) FROM posts WHERE date_str = ?", (target_window,))
    target_count = cur.fetchone()[0]
    conn.close()

    log_operation('move_window', {
        'link_id': link_id,
        'old_window': old_window,
        'old_daily_no': old_daily_no,
        'new_window': target_window,
        'target_count': target_count
    })

    resp, status = proxy_request('POST', f'/api/post/{link_id}/move-window', data)
    if not resp.get('success', False):
        # 补充错误信息
        if not resp.get('error'):
            resp['error'] = '移动失败，请检查目标窗口是否存在或帖子是否已被移动。'
        log_operation('move_window_failed', {
            'link_id': link_id,
            'target_window': target_window,
            'error': resp.get('error')
        })
    return jsonify(resp), status

@yard_bp.route('/api/post/<int:link_id>/comment', methods=['POST'])
def api_update_comment(link_id):
    data = request.get_json() or {}
    new_comment = data.get('comment', '').strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT comment, model_used, analyzed_at FROM llm_analyses WHERE link_id = ?", (link_id,))
    row = cur.fetchone()
    old_comment = row[0] if row else ''
    old_model = row[1] if row else None
    old_analyzed_at = row[2] if row else None
    conn.close()

    log_operation('edit_comment', {
        'link_id': link_id,
        'old_comment': old_comment,
        'new_comment': new_comment,
        'old_model': old_model,
        'old_analyzed_at': old_analyzed_at
    })

    resp, status = proxy_request('POST', f'/api/post/{link_id}/comment', data)
    if not resp.get('success', False):
        log_operation('edit_comment_failed', {'link_id': link_id, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/post/<int:link_id>/analyze', methods=['POST'])
def api_analyze_post(link_id):
    log_operation('generate_comment', {'link_id': link_id})
    resp, status = proxy_request('POST', f'/api/post/{link_id}/analyze', timeout=120)
    if not resp.get('success', False):
        log_operation('generate_comment_failed', {'link_id': link_id, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/fetch-feed', methods=['POST'])
def api_fetch_feed():
    log_operation('fetch_feed', {'window': get_current_window_no()})
    resp, status = proxy_request('POST', '/api/fetch-feed')
    if not resp.get('success', False):
        log_operation('fetch_feed_failed', {'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/fetch-at', methods=['POST'])
def api_fetch_at():
    log_operation('fetch_at', {'window': get_current_window_no()})
    resp, status = proxy_request('POST', '/api/fetch-at')
    if not resp.get('success', False):
        log_operation('fetch_at_failed', {'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/generate-report', methods=['POST'])
def api_generate_report():
    data = request.get_json() or {}
    window_no = data.get('window_no', get_current_window_no())
    log_operation('generate_report', {'window_no': window_no})
    resp, status = proxy_request('POST', '/api/generate-report', data)
    if not resp.get('success', False):
        log_operation('generate_report_failed', {'window_no': window_no, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/reset-order', methods=['POST'])
def api_reset_order():
    data = request.get_json() or {}
    window_no = data.get('window_no')
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT link_id, daily_no FROM posts WHERE date_str = ? ORDER BY daily_no", (window_no,))
    rows = cur.fetchall()
    old_order = [{'link_id': row[0], 'daily_no': row[1]} for row in rows]
    conn.close()

    log_operation('reset_order', {
        'window_no': window_no,
        'old_order': old_order
    })

    resp, status = proxy_request('POST', '/api/reset-order', data)
    if not resp.get('success', False):
        log_operation('reset_order_failed', {'window_no': window_no, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/post/<int:link_id>/delete-analysis', methods=['POST'])
def api_delete_analysis(link_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT comment, model_used, analyzed_at FROM llm_analyses WHERE link_id = ?", (link_id,))
    row = cur.fetchone()
    old_comment = row[0] if row else None
    old_model = row[1] if row else None
    old_analyzed_at = row[2] if row else None
    conn.close()

    log_operation('delete_analysis', {
        'link_id': link_id,
        'old_comment': old_comment,
        'old_model': old_model,
        'old_analyzed_at': old_analyzed_at
    })

    resp, status = proxy_request('POST', f'/api/post/{link_id}/delete-analysis')
    if not resp.get('success', False):
        log_operation('delete_analysis_failed', {'link_id': link_id, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/analyze-window', methods=['POST'])
def api_analyze_window():
    data = request.get_json() or {}
    window_no = data.get('window_no', get_current_window_no())
    log_operation('analyze_window', {'window_no': window_no})
    resp, status = proxy_request('POST', '/api/analyze-window', data)
    if not resp.get('success', False):
        log_operation('analyze_window_failed', {'window_no': window_no, 'error': resp.get('error')})
    return jsonify(resp), status

# ========== 总评 API ==========
@yard_bp.route('/api/summary', methods=['GET'])
def api_get_summary():
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
    data = request.get_json() or {}
    window_no = data.get('window_no') or get_current_window_no()
    new_comment = data.get('comment', '').strip()

    window_start, _ = get_window_by_no(window_no)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT comment FROM llm_analyses WHERE daily_no='SUMMARY' AND window_start=?", (window_start,))
    row = cur.fetchone()
    old_comment = row[0] if row else ''
    conn.close()

    log_operation('update_summary', {
        'window_no': window_no,
        'old_comment': old_comment,
        'new_comment': new_comment
    })

    resp, status = proxy_request('POST', '/api/summary/update', data)
    if not resp.get('success', False):
        log_operation('update_summary_failed', {'window_no': window_no, 'error': resp.get('error')})
    return jsonify(resp), status

@yard_bp.route('/api/summary/generate', methods=['POST'])
def api_generate_summary():
    data = request.get_json() or {}
    window_no = data.get('window_no', get_current_window_no())
    log_operation('generate_summary', {'window_no': window_no})
    resp, status = proxy_request('POST', '/api/summary/generate', data)
    if not resp.get('success', False):
        log_operation('generate_summary_failed', {'window_no': window_no, 'error': resp.get('error')})
    return jsonify(resp), status

# ========== 操作日志查询 ==========
@yard_bp.route('/api/logs')
def api_logs():
    if not session.get('authorized') or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    date_str = request.args.get('date')
    page = request.args.get('page', 1, type=int)
    per_page = 20

    if date_str:
        try:
            local_dt = datetime.strptime(date_str, '%Y-%m-%d')
            start = int(local_dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
            end = int((local_dt + timedelta(days=1)).replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
        except:
            start, end = 0, int(time.time()) + 86400
    else:
        today = datetime.now().astimezone(timezone(timedelta(hours=8))).date()
        start = int(datetime(today.year, today.month, today.day, tzinfo=timezone(timedelta(hours=8))).timestamp())
        end = start + 86400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM operation_log WHERE timestamp >= ? AND timestamp < ?", (start, end))
    total = cur.fetchone()[0]

    offset = (page - 1) * per_page
    cur.execute(
        "SELECT id, user_nickname, user_ip, device_id, operation_type, detail, timestamp "
        "FROM operation_log WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (start, end, per_page, offset)
    )
    rows = cur.fetchall()
    conn.close()

    items = []
    link_ids = set()
    for row in rows:
        try:
            detail = json.loads(row[5]) if row[5] else {}
        except:
            detail = row[5] if row[5] else {}
        items.append({
            'id': row[0],
            'nickname': row[1],
            'ip': row[2],
            'device_id': row[3],
            'operation_type': row[4],
            'detail': detail,
            'timestamp': row[6]
        })
        # 收集需要查询的 link_id（兼容字符串和整数）
        if isinstance(detail, dict):
            if 'link_id' in detail:
                try:
                    link_ids.add(int(detail['link_id']))
                except (ValueError, TypeError):
                    pass
            if 'target_link_id' in detail:
                try:
                    link_ids.add(int(detail['target_link_id']))
                except (ValueError, TypeError):
                    pass

    # 查询帖子标题、作者、内容预览
    post_map = {}
    if link_ids:
        conn = get_db()
        cur = conn.cursor()
        placeholders = ','.join('?' * len(link_ids))
        cur.execute(f"""
            SELECT link_id, daily_no, title, username, content 
            FROM posts WHERE link_id IN ({placeholders})
        """, tuple(link_ids))
        for row in cur.fetchall():
            content_preview = row[4] or ''
            if len(content_preview) > 120:
                content_preview = content_preview[:120] + '...'
            post_map[row[0]] = {
                'link_id': row[0],
                'daily_no': row[1] or '--',
                'title': row[2] or '(无标题)',
                'username': row[3] or '匿名',
                'content_preview': content_preview
            }
        conn.close()

    # 合并帖子信息到对应日志项
    for item in items:
        d = item.get('detail', {})
        if isinstance(d, dict):
            if 'link_id' in d and d['link_id'] in post_map:
                item['post'] = post_map[d['link_id']]
            if 'target_link_id' in d and d['target_link_id'] in post_map:
                item['target_post'] = post_map[d['target_link_id']]

    return jsonify({
        'items': items,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page
    })

# ========== 白名单与封禁管理 ==========
def parse_duration(duration_str):
    duration_str = duration_str.lower().strip()
    if duration_str.endswith('h'):
        try:
            hours = int(duration_str[:-1])
            return timedelta(hours=hours)
        except:
            pass
    elif duration_str.endswith('m'):
        try:
            minutes = int(duration_str[:-1])
            return timedelta(minutes=minutes)
        except:
            pass
    return timedelta(hours=1)

@yard_bp.route('/whitelist')
def whitelist():
    if not session.get('authorized') or session.get('role') != 'admin':
        abort(403)

    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    cur = conn.cursor()

    five_min_ago = (datetime.now() - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
    cur.execute('''
        SELECT COUNT(DISTINCT ip) as online
        FROM access_whitelist
        WHERE date = ? AND last_access > ?
    ''', (today, five_min_ago))
    online_count = cur.fetchone()[0]

    cur.execute('''
        SELECT id, ip, first_access, last_access, user_agent, path,
               user_identifier, device_id, nickname
        FROM access_whitelist
        WHERE date = ? AND authorized = 1
        ORDER BY first_access DESC
    ''', (today,))
    whitelist_entries = [dict(row) for row in cur.fetchall()]

    now = datetime.now().isoformat()
    cur.execute('''
        SELECT id, ip, failed_attempts, last_fail_time, blocked_until, note
        FROM ip_blocklist
        WHERE blocked_until > ?
        ORDER BY blocked_until DESC
    ''', (now,))
    blocked_entries = [dict(row) for row in cur.fetchall()]

    conn.close()
    return render_template('whitelist.html',
        today=today,
        whitelist=whitelist_entries,
        blocked=blocked_entries,
        online=online_count,
        now=datetime.now()
    )

@yard_bp.route('/whitelist/update-identifier', methods=['POST'])
def update_identifier():
    if not session.get('authorized') or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    record_id = data.get('id')
    new_identifier = data.get('identifier', '').strip()

    if not record_id:
        return jsonify({'success': False, 'error': '缺少记录 ID'}), 400

    if not new_identifier:
        new_identifier = f"UID-{secrets.token_hex(4).upper()}"

    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE access_whitelist SET user_identifier = ? WHERE id = ?', (new_identifier, record_id))
    affected = cur.rowcount
    conn.commit()
    conn.close()

    if affected:
        return jsonify({'success': True, 'identifier': new_identifier})
    else:
        return jsonify({'success': False, 'error': '记录不存在'}), 404

@yard_bp.route('/whitelist/batch-merge', methods=['POST'])
def batch_merge():
    if not session.get('authorized') or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    ids = data.get('ids', [])
    target_uid = data.get('target_identifier', '').strip()

    if not ids:
        return jsonify({'success': False, 'error': '请选择至少一条记录'}), 400
    if not target_uid:
        return jsonify({'success': False, 'error': '请输入目标标识符'}), 400

    conn = get_db()
    cur = conn.cursor()
    placeholders = ','.join('?' * len(ids))
    cur.execute(f'UPDATE access_whitelist SET user_identifier = ? WHERE id IN ({placeholders})', [target_uid] + ids)
    affected = cur.rowcount
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'merged': affected})

@yard_bp.route('/whitelist/delete/<int:id>', methods=['POST'])
def whitelist_delete(id):
    if not session.get('authorized') or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT ip FROM ip_blocklist WHERE id = ?", (id,))
    row = cur.fetchone()
    ip = row[0] if row else None
    conn.close()

    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM ip_blocklist WHERE id = ?', (id,))
    conn.commit()
    affected = cur.rowcount
    conn.close()

    if affected:
        log_operation('unblock_ip', {'ip': ip})
        return jsonify({'success': True, 'message': '已解封'})
    else:
        return jsonify({'success': False, 'error': '记录不存在'}), 404

@yard_bp.route('/whitelist/block', methods=['POST'])
def whitelist_block():
    if not session.get('authorized') or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    data = request.get_json() or {}
    ip = data.get('ip', '').strip()
    duration_str = data.get('duration', '1h').strip()
    if not ip:
        return jsonify({'success': False, 'error': 'IP 不能为空'}), 400

    import re
    if not re.match(r'^(\d{1,3}\.){3}\d{1,3}$', ip):
        return jsonify({'success': False, 'error': '无效的 IP 格式'}), 400

    delta = parse_duration(duration_str)
    blocked_until = datetime.now() + delta

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM ip_blocklist WHERE ip = ?', (ip,))
    row = cur.fetchone()
    if row:
        cur.execute('''
            UPDATE ip_blocklist
            SET blocked_until = ?, failed_attempts = 0, last_fail_time = NULL, note = '手动封禁'
            WHERE ip = ?
        ''', (blocked_until.isoformat(), ip))
    else:
        cur.execute('''
            INSERT INTO ip_blocklist (ip, failed_attempts, last_fail_time, blocked_until, note)
            VALUES (?, 0, NULL, ?, '手动封禁')
        ''', (ip, blocked_until.isoformat()))
    conn.commit()
    conn.close()

    log_operation('block_ip', {'ip': ip, 'duration': duration_str})
    return jsonify({'success': True, 'message': f'IP {ip} 已封禁至 {blocked_until.strftime("%Y-%m-%d %H:%M")}'})

@yard_bp.route('/whitelist/force-logout', methods=['POST'])
def force_logout():
    if not session.get('authorized') or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE access_whitelist SET authorized = 0 WHERE date = ? AND authorized = 1', (today,))
    affected = cur.rowcount
    conn.commit()
    conn.close()

    log_operation('force_logout', {'today': today})
    return jsonify({'success': True, 'message': f'已强制 {affected} 条授权记录重新登录'})

@yard_bp.route('/api/online')
def api_online():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT COUNT(DISTINCT ip) as online
        FROM access_whitelist
        WHERE last_access > datetime('now', '-45 seconds')
    ''')
    count = cur.fetchone()[0]
    conn.close()
    return jsonify({'online': count})

@yard_bp.route('/download/<filename>')
def download_report(filename):
    reports_dir = Path(get_reports_dir())
    safe_path = reports_dir / filename
    try:
        if not safe_path.resolve().is_relative_to(reports_dir.resolve()):
            abort(403)
    except AttributeError:
        try:
            safe_path.resolve().relative_to(reports_dir.resolve())
        except ValueError:
            abort(403)
    if not safe_path.exists() or not safe_path.is_file():
        abort(404)
    return send_file(safe_path, as_attachment=True, download_name=filename)
