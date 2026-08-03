# -*- coding: utf-8 -*-
from flask import Flask, send_file, jsonify, request, session, redirect, render_template
import os
from datetime import datetime, timedelta
import sqlite3
import secrets
import time  # [新增] 用于日志时间戳
from werkzeug.middleware.proxy_fix import ProxyFix

from mcserver import monitor as mc_monitor
from flask import Blueprint

app = Flask(__name__)

# ========== 配置读取（从环境变量） ==========
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
ACCESS_PASSWORD = os.environ.get('HEIBOXYARD_ACCESS_PASSWORD', 'default123')
ADMIN_PASSWORD = os.environ.get('HEIBOXYARD_ADMIN_PASSWORD', 'admin456')
DB_PATH = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/posts.db"

app.secret_key = SECRET_KEY
app.config['ACCESS_PASSWORD'] = ACCESS_PASSWORD
app.config['ADMIN_PASSWORD'] = ADMIN_PASSWORD
app.config['HEIBOXYARD_DB_PATH'] = DB_PATH

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# ========== 原有目录配置 ==========
GROUP_REPORTS_DIR = "/home/admin/qqbot/astrbot_data/plugin_data/astrbot_plugin_qq_group_daily_analysis/self_hosted_html_reports"
YARD_REPORTS_DIR = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/reports"
YARD_IMAGES_DIR = "/home/admin/qqbot/astrbot_data/plugin_data/heiboxyard/images"

# ========== 数据库初始化与辅助函数 ==========
def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_conn()
    # 白名单表
    conn.execute('''
        CREATE TABLE IF NOT EXISTS access_whitelist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            date TEXT NOT NULL,
            authorized INTEGER DEFAULT 1,
            first_access TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_access TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_agent TEXT,
            path TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ip_date ON access_whitelist(ip, date)')

    # IP 封禁表
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ip_blocklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL UNIQUE,
            failed_attempts INTEGER DEFAULT 0,
            last_fail_time TIMESTAMP,
            blocked_until TIMESTAMP,
            note TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_blocked_ip ON ip_blocklist(ip)')

    # 列迁移
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(access_whitelist)")
    columns = [col[1] for col in cur.fetchall()]

    if 'user_identifier' not in columns:
        conn.execute('ALTER TABLE access_whitelist ADD COLUMN user_identifier TEXT')
        print("✅ 已添加 user_identifier 列")

    if 'device_id' not in columns:
        conn.execute('ALTER TABLE access_whitelist ADD COLUMN device_id TEXT')
        print("✅ 已添加 device_id 列")

    if 'nickname' not in columns:
        conn.execute('ALTER TABLE access_whitelist ADD COLUMN nickname TEXT')
        print("✅ 已添加 nickname 列")

    # 为历史数据补全随机 UID
    cur.execute("SELECT id FROM access_whitelist WHERE user_identifier IS NULL")
    rows = cur.fetchall()
    for (row_id,) in rows:
        new_uid = f"UID-{secrets.token_hex(4).upper()}"
        conn.execute("UPDATE access_whitelist SET user_identifier = ? WHERE id = ?", (new_uid, row_id))

    # [新增] 创建操作日志表（确保与 yard_admin 一致）
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

# ========== [新增] 操作日志记录函数（与 yard_admin 保持一致） ==========
def log_operation(operation_type, detail=None, nickname=None, ip=None, device_id=None):
    """记录操作日志（用于主认证服务）"""
    if nickname is None:
        nickname = session.get('nickname', 'unknown')
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

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO operation_log (user_nickname, user_ip, device_id, operation_type, detail, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (nickname, ip, device_id, operation_type, detail_str, int(time.time()))
    )
    conn.commit()
    conn.close()

def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        ip = forwarded.split(',')[0].strip()
    else:
        ip = request.remote_addr
    if ip in ('127.0.0.1', '::1') or ip.startswith('10.') or ip.startswith('192.168.'):
        real_ip = request.headers.get('X-Real-IP')
        if real_ip:
            ip = real_ip
    return ip

def is_ip_blocked(ip):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('SELECT blocked_until FROM ip_blocklist WHERE ip=?', (ip,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        blocked_until = datetime.fromisoformat(row[0].replace(' ', 'T'))
        if blocked_until > datetime.now():
            return True
        else:
            conn = get_db_conn()
            conn.execute('DELETE FROM ip_blocklist WHERE ip=?', (ip,))
            conn.commit()
            conn.close()
            return False
    return False

def record_failed_attempt(ip):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, failed_attempts, blocked_until FROM ip_blocklist WHERE ip=?', (ip,))
    row = cur.fetchone()
    now = datetime.now()
    if row:
        attempts = row[1] + 1
        if attempts >= 5:
            blocked_until = now + timedelta(hours=1)
            conn.execute('''
                UPDATE ip_blocklist SET failed_attempts=?, last_fail_time=?, blocked_until=?
                WHERE ip=?
            ''', (attempts, now.isoformat(), blocked_until.isoformat(), ip))
        else:
            conn.execute('''
                UPDATE ip_blocklist SET failed_attempts=?, last_fail_time=?
                WHERE ip=?
            ''', (attempts, now.isoformat(), ip))
    else:
        conn.execute('''
            INSERT INTO ip_blocklist (ip, failed_attempts, last_fail_time)
            VALUES (?, 1, ?)
        ''', (ip, now.isoformat()))
    conn.commit()
    conn.close()

def is_ip_authorized(ip):
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM access_whitelist WHERE ip=? AND date=? AND authorized=1 ORDER BY id DESC LIMIT 1', (ip, today))
    row = cur.fetchone()
    conn.close()
    return row is not None

# ========== 核心函数：授权 + 写入昵称 / device_id（修复版） ==========
def authorize_ip(ip, user_agent, path, device_id=None, nickname=None):
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"[DEBUG] authorize_ip: ip={ip}, device_id={device_id}, nickname={nickname}")
    conn = get_db_conn()
    cur = conn.cursor()

    # 1. 优先按 device_id 查找（不限制 authorized 状态，找到就更新）
    if device_id:
        cur.execute('''
            SELECT id, user_identifier, nickname
            FROM access_whitelist
            WHERE device_id=? AND date=?
        ''', (device_id, today))
        row = cur.fetchone()
        if row:
            cur.execute('''
                UPDATE access_whitelist
                SET last_access=CURRENT_TIMESTAMP,
                    ip=?,
                    user_agent=?,
                    path=?,
                    nickname=COALESCE(?, nickname),
                    authorized = 1
                WHERE id=?
            ''', (ip, user_agent, path, nickname, row[0]))
            print(f"[DEBUG] Updated by device_id {device_id}, set authorized=1")
            conn.commit()
            conn.close()
            return

    # 2. 按 IP 查找（不限制 authorized 状态）
    cur.execute('''
        SELECT id, user_identifier, nickname
        FROM access_whitelist
        WHERE ip=? AND date=?
    ''', (ip, today))
    row = cur.fetchone()

    if row:
        cur.execute('''
            UPDATE access_whitelist
            SET last_access=CURRENT_TIMESTAMP,
                user_agent=?,
                path=?,
                device_id=COALESCE(?, device_id),
                nickname=COALESCE(?, nickname),
                authorized = 1
            WHERE id=?
        ''', (user_agent, path, device_id, nickname, row[0]))
        print(f"[DEBUG] Updated by IP {ip}, set authorized=1")
    else:
        # 3. 完全不存在 → 插入新记录（默认 authorized=1）
        new_uid = f"UID-{secrets.token_hex(4).upper()}"
        cur.execute('''
            INSERT INTO access_whitelist
                (ip, date, user_agent, path, user_identifier, device_id, nickname)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (ip, today, user_agent, path, new_uid, device_id, nickname))
        print(f"[DEBUG] Inserted new record for IP {ip}, UID={new_uid}")

    conn.commit()
    conn.close()

# ========== 文件大小/时间辅助（原有，未变） ==========
def get_file_size(path):
    size = os.path.getsize(path)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def get_file_time(path):
    mtime = os.path.getmtime(path)
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

def scan_reports(report_dir, route_prefix):
    if not os.path.exists(report_dir):
        return [], 0, "0 B"
    report_files = [f for f in os.listdir(report_dir)
                    if f.lower().endswith(('.html', '.jpg', '.jpeg', '.png')) and f != 'index.html']
    report_files.sort(key=lambda x: os.path.getmtime(os.path.join(report_dir, x)), reverse=True)
    items = []
    total_size = 0
    for f in report_files:
        file_path = os.path.join(report_dir, f)
        size = get_file_size(file_path)
        mtime = get_file_time(file_path)
        total_size += os.path.getsize(file_path)
        ext = os.path.splitext(f)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png'):
            icon = '&#127748;'
            route = route_prefix + '/image/' + f
            ftype = '图片报告'
        else:
            icon = '&#128202;'
            route = route_prefix + '/view/' + f
            ftype = 'HTML报告'
        items.append('<li class="report-item"><a href="' + route + '" class="report-link"><span class="file-icon">' + icon + '</span><div class="file-info"><div class="file-name">' + f + '</div><div class="file-meta"><span>&#128336; ' + mtime + '</span><span class="file-size">&#128230; ' + size + '</span><span class="file-size">' + ftype + '</span></div></div><span class="arrow">&#10142;</span></a></li>')
    size_tmp = total_size
    total_size_str = "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_tmp < 1024.0:
            total_size_str = f"{size_tmp:.1f} {unit}"
            break
        size_tmp /= 1024.0
    else:
        total_size_str = f"{size_tmp:.1f} TB"
    return items, len(report_files), total_size_str

def build_index_page(title, subtitle, icon, css, items, total_count, total_size,
                     other_title=None, other_link=None, other_desc=None):
    items_html = ''.join(items) if items else '<div class="empty-state"><div class="empty-icon">&#128235;</div><p>还没有报告文件哦 ~</p><p style="margin-top: 10px; font-size: 0.9em;">等待数据生成中...</p></div>'
    nav_html = ''
    if other_link and other_title:
        nav_html = '<div style="text-align: center; margin-bottom: 30px;"><a href="' + other_link + '" style="display: inline-block; padding: 12px 24px; background: linear-gradient(135deg, var(--primary), var(--secondary)); color: white; text-decoration: none; border-radius: 25px; font-size: 1em; transition: all 0.3s ease; box-shadow: 0 4px 15px var(--shadow);" onmouseover="this.style.transform=\'scale(1.05)\'" onmouseout="this.style.transform=\'scale(1)\'">' + other_desc + ' &rarr;</a></div>'
    return '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n    <meta charset="UTF-8">\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n    <title>' + icon + ' ' + title + ' ' + icon + '</title>\n    <style>' + css + '</style>\n</head>\n<body>\n    <div class="container">\n        <div class="header">\n            <h1>' + icon + ' ' + title + ' ' + icon + '</h1>\n            <p class="subtitle">' + subtitle + '</p>\n        </div>\n        ' + nav_html + '\n        <div class="stats">\n            <div class="stat-item">\n                <div class="stat-number">' + str(total_count) + '</div>\n                <div class="stat-label">&#128209; 报告总数</div>\n            </div>\n            <div class="stat-item">\n                <div class="stat-number">' + total_size + '</div>\n                <div class="stat-label">&#128190; 占用空间</div>\n            </div>\n            <div class="stat-item">\n                <div class="stat-number">' + datetime.now().strftime("%m-%d") + '</div>\n                <div class="stat-label">&#128197; 今日日期</div>\n            </div>\n        </div>\n        <ul class="report-list">\n            ' + items_html + '\n        </ul>\n        <div class="footer">\n            <p>Made with <span class="heart">&#9829;</span> for QQ Group Analysis</p>\n            <p style="margin-top: 5px; font-size: 0.8em;">AstrBot Plugin | 二次元风格主题</p>\n        </div>\n    </div>\n</body>\n</html>'

# ========== CSS（原有，未变） ==========
ANIME_CSS = """
:root {
    --primary: #ff6b9d;
    --secondary: #c44569;
    --accent: #f8b500;
    --bg-dark: #1a1a2e;
    --bg-card: #16213e;
    --bg-hover: #0f3460;
    --text-main: #eaeaea;
    --text-muted: #a0a0a0;
    --border: #e94560;
    --shadow: rgba(233, 69, 96, 0.3);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    background: linear-gradient(135deg, var(--bg-dark) 0%, #16213e 50%, #0f3460 100%);
    background-attachment: fixed;
    color: var(--text-main);
    min-height: 100vh;
    position: relative;
    overflow-x: hidden;
}
body::before {
    content: '';
    position: fixed;
    top: 0; left: 0; width: 100%; height: 100%;
    background-image: 
        radial-gradient(circle at 20% 50%, rgba(255, 107, 157, 0.1) 0%, transparent 50%),
        radial-gradient(circle at 80% 80%, rgba(196, 69, 105, 0.1) 0%, transparent 50%),
        radial-gradient(circle at 40% 20%, rgba(248, 181, 0, 0.05) 0%, transparent 50%);
    pointer-events: none;
    z-index: -1;
}
.container { max-width: 900px; margin: 0 auto; padding: 40px 20px; }
.header {
    text-align: center;
    margin-bottom: 40px;
    padding: 30px;
    background: linear-gradient(135deg, rgba(255, 107, 157, 0.15), rgba(196, 69, 105, 0.15));
    border-radius: 20px;
    border: 2px solid var(--border);
    box-shadow: 0 0 30px var(--shadow);
    position: relative;
    overflow: hidden;
}
.header::before { content: '\u273F'; position: absolute; top: 10px; left: 20px; font-size: 24px; color: var(--primary); opacity: 0.6; animation: float 3s ease-in-out infinite; }
.header::after { content: '\u273F'; position: absolute; bottom: 10px; right: 20px; font-size: 24px; color: var(--accent); opacity: 0.6; animation: float 3s ease-in-out infinite reverse; }
@keyframes float { 0%, 100% { transform: translateY(0px); } 50% { transform: translateY(-10px); } }
h1 {
    font-size: 2.5em;
    background: linear-gradient(45deg, var(--primary), var(--accent));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 10px;
    letter-spacing: 2px;
}
.subtitle { color: var(--text-muted); font-size: 1.1em; letter-spacing: 1px; }
.stats { display: flex; justify-content: center; gap: 30px; margin-bottom: 30px; flex-wrap: wrap; }
.stat-item {
    background: var(--bg-card);
    padding: 15px 25px;
    border-radius: 15px;
    border: 1px solid rgba(233, 69, 96, 0.3);
    text-align: center;
    transition: all 0.3s ease;
}
.stat-item:hover { transform: translateY(-3px); box-shadow: 0 5px 20px var(--shadow); border-color: var(--primary); }
.stat-number { font-size: 1.8em; font-weight: bold; color: var(--primary); }
.stat-label { font-size: 0.9em; color: var(--text-muted); margin-top: 5px; }
.report-list { list-style: none; }
.report-item {
    background: linear-gradient(135deg, var(--bg-card), rgba(22, 33, 62, 0.8));
    margin-bottom: 15px;
    border-radius: 15px;
    border: 1px solid rgba(233, 69, 96, 0.2);
    transition: all 0.3s ease;
    overflow: hidden;
    position: relative;
}
.report-item::before { content: ''; position: absolute; left: 0; top: 0; height: 100%; width: 4px; background: linear-gradient(to bottom, var(--primary), var(--accent)); opacity: 0; transition: opacity 0.3s ease; }
.report-item:hover { transform: translateX(5px); border-color: var(--primary); box-shadow: 0 5px 25px var(--shadow); }
.report-item:hover::before { opacity: 1; }
.report-link { display: flex; align-items: center; padding: 20px 25px; color: var(--text-main); text-decoration: none; gap: 15px; }
.file-icon { font-size: 2em; flex-shrink: 0; filter: drop-shadow(0 0 5px rgba(255, 107, 157, 0.5)); }
.file-info { flex: 1; min-width: 0; }
.file-name { font-size: 1.1em; font-weight: 500; margin-bottom: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-meta { font-size: 0.85em; color: var(--text-muted); display: flex; gap: 15px; align-items: center; }
.file-size { background: rgba(255, 107, 157, 0.15); padding: 2px 10px; border-radius: 10px; font-size: 0.8em; color: var(--primary); }
.arrow { font-size: 1.5em; color: var(--primary); opacity: 0; transition: all 0.3s ease; transform: translateX(-10px); }
.report-item:hover .arrow { opacity: 1; transform: translateX(0); }
.empty-state { text-align: center; padding: 60px 20px; color: var(--text-muted); }
.empty-icon { font-size: 4em; margin-bottom: 20px; opacity: 0.5; }
.footer { text-align: center; margin-top: 40px; padding: 20px; color: var(--text-muted); font-size: 0.9em; border-top: 1px solid rgba(233, 69, 96, 0.2); }
.heart { color: var(--primary); animation: heartbeat 1.5s ease-in-out infinite; display: inline-block; }
@keyframes heartbeat { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.2); } }
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: var(--bg-dark); }
::-webkit-scrollbar-thumb { background: linear-gradient(var(--primary), var(--secondary)); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--primary); }
@media (max-width: 600px) { h1 { font-size: 1.8em; } .report-link { padding: 15px; } .file-icon { font-size: 1.5em; } .stats { gap: 15px; } .stat-item { padding: 10px 15px; } }
"""

YARD_CSS = """
:root {
    --primary: #4ade80;
    --secondary: #22c55e;
    --accent: #a3e635;
    --bg-dark: #0f172a;
    --bg-card: #1e293b;
    --bg-hover: #334155;
    --text-main: #f1f5f9;
    --text-muted: #94a3b8;
    --border: #4ade80;
    --shadow: rgba(74, 222, 128, 0.3);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    background: linear-gradient(135deg, var(--bg-dark) 0%, #1e293b 50%, #0f172a 100%);
    background-attachment: fixed;
    color: var(--text-main);
    min-height: 100vh;
    position: relative;
    overflow-x: hidden;
}
body::before {
    content: '';
    position: fixed;
    top: 0; left: 0; width: 100%; height: 100%;
    background-image: 
        radial-gradient(circle at 20% 50%, rgba(74, 222, 128, 0.1) 0%, transparent 50%),
        radial-gradient(circle at 80% 80%, rgba(34, 197, 94, 0.1) 0%, transparent 50%),
        radial-gradient(circle at 40% 20%, rgba(163, 230, 53, 0.05) 0%, transparent 50%);
    pointer-events: none;
    z-index: -1;
}
.container { max-width: 900px; margin: 0 auto; padding: 40px 20px; }
.header {
    text-align: center;
    margin-bottom: 40px;
    padding: 30px;
    background: linear-gradient(135deg, rgba(74, 222, 128, 0.15), rgba(34, 197, 94, 0.15));
    border-radius: 20px;
    border: 2px solid var(--border);
    box-shadow: 0 0 30px var(--shadow);
    position: relative;
    overflow: hidden;
}
.header::before { content: '\u273F'; position: absolute; top: 10px; left: 20px; font-size: 24px; color: var(--primary); opacity: 0.6; animation: float 3s ease-in-out infinite; }
.header::after { content: '\u273F'; position: absolute; bottom: 10px; right: 20px; font-size: 24px; color: var(--accent); opacity: 0.6; animation: float 3s ease-in-out infinite reverse; }
@keyframes float { 0%, 100% { transform: translateY(0px); } 50% { transform: translateY(-10px); } }
h1 {
    font-size: 2.5em;
    background: linear-gradient(45deg, var(--primary), var(--accent));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 10px;
    letter-spacing: 2px;
}
.subtitle { color: var(--text-muted); font-size: 1.1em; letter-spacing: 1px; }
.stats { display: flex; justify-content: center; gap: 30px; margin-bottom: 30px; flex-wrap: wrap; }
.stat-item {
    background: var(--bg-card);
    padding: 15px 25px;
    border-radius: 15px;
    border: 1px solid rgba(74, 222, 128, 0.3);
    text-align: center;
    transition: all 0.3s ease;
}
.stat-item:hover { transform: translateY(-3px); box-shadow: 0 5px 20px var(--shadow); border-color: var(--primary); }
.stat-number { font-size: 1.8em; font-weight: bold; color: var(--primary); }
.stat-label { font-size: 0.9em; color: var(--text-muted); margin-top: 5px; }
.report-list { list-style: none; }
.report-item {
    background: linear-gradient(135deg, var(--bg-card), rgba(30, 41, 59, 0.8));
    margin-bottom: 15px;
    border-radius: 15px;
    border: 1px solid rgba(74, 222, 128, 0.2);
    transition: all 0.3s ease;
    overflow: hidden;
    position: relative;
}
.report-item::before { content: ''; position: absolute; left: 0; top: 0; height: 100%; width: 4px; background: linear-gradient(to bottom, var(--primary), var(--accent)); opacity: 0; transition: opacity 0.3s ease; }
.report-item:hover { transform: translateX(5px); border-color: var(--primary); box-shadow: 0 5px 25px var(--shadow); }
.report-item:hover::before { opacity: 1; }
.report-link { display: flex; align-items: center; padding: 20px 25px; color: var(--text-main); text-decoration: none; gap: 15px; }
.file-icon { font-size: 2em; flex-shrink: 0; filter: drop-shadow(0 0 5px rgba(74, 222, 128, 0.5)); }
.file-info { flex: 1; min-width: 0; }
.file-name { font-size: 1.1em; font-weight: 500; margin-bottom: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-meta { font-size: 0.85em; color: var(--text-muted); display: flex; gap: 15px; align-items: center; }
.file-size { background: rgba(74, 222, 128, 0.15); padding: 2px 10px; border-radius: 10px; font-size: 0.8em; color: var(--primary); }
.arrow { font-size: 1.5em; color: var(--primary); opacity: 0; transition: all 0.3s ease; transform: translateX(-10px); }
.report-item:hover .arrow { opacity: 1; transform: translateX(0); }
.empty-state { text-align: center; padding: 60px 20px; color: var(--text-muted); }
.empty-icon { font-size: 4em; margin-bottom: 20px; opacity: 0.5; }
.footer { text-align: center; margin-top: 40px; padding: 20px; color: var(--text-muted); font-size: 0.9em; border-top: 1px solid rgba(74, 222, 128, 0.2); }
.heart { color: var(--primary); animation: heartbeat 1.5s ease-in-out infinite; display: inline-block; }
@keyframes heartbeat { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.2); } }
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: var(--bg-dark); }
::-webkit-scrollbar-thumb { background: linear-gradient(var(--primary), var(--secondary)); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--primary); }
@media (max-width: 600px) { h1 { font-size: 1.8em; } .report-link { padding: 15px; } .file-icon { font-size: 1.5em; } .stats { gap: 15px; } .stat-item { padding: 10px 15px; } }
"""

MOBILE_FIX_CSS = '''<style id="mobile-auto-fix">''' + '''
@media (max-width: 768px) {
    html { font-size: 14px !important; }
    body { padding: 8px !important; }
    .container { max-width: 100% !important; width: 100% !important; padding: 15px !important; border-width: 1px !important; box-shadow: 2px 2px 0 var(--color-blue), 4px 4px 0 var(--color-pink) !important; }
    .title-sticker { padding: 12px 20px !important; }
    .title-sticker h1 { font-size: 1.4rem !important; -webkit-text-stroke: 0.5px !important; }
    .date-badge { font-size: 0.9rem !important; }
    .stats-wrapper { flex-direction: column !important; gap: 15px !important; }
    .stats-grid { grid-template-columns: repeat(2, 1fr) !important; gap: 10px !important; }
    .stamp { padding: 10px !important; }
    .stamp-num { font-size: 1.4rem !important; }
    .stamp-label { font-size: 0.85rem !important; }
    .highlight-section { padding: 15px !important; }
    .time-big { font-size: 1.6rem !important; }
    .time-desc { font-size: 1rem !important; }
    .chart-section { padding: 15px !important; }
    .chart-section-horizontal { height: 140px !important; padding: 10px 5px !important; }
    .chart-label-xaxis { font-size: 0.6rem !important; }
    .chart-value-top { font-size: 0.6rem !important; top: -14px !important; }
    .topic-section { padding: 20px 15px 20px 35px !important; }
    .topic-title { font-size: 1.1rem !important; }
    .topic-detail { font-size: 0.95rem !important; }
    .user-capsule { font-size: 0.8em !important; }
    .masonry-grid { column-count: 1 !important; }
    .user-card { margin-bottom: 15px !important; transform: none !important; padding: 15px !important; }
    .u-name { font-size: 1rem !important; }
    .u-reason { font-size: 0.9rem !important; }
    .u-avatar { width: 45px !important; height: 45px !important; }
    .quote-wrapper { max-width: 100% !important; margin-bottom: 20px !important; }
    .q-flex-container { flex-direction: column !important; align-items: center !important; gap: 8px !important; }
    .q-user-col { width: 50px !important; }
    .q-avatar { width: 45px !important; height: 45px !important; }
    .q-content-col { align-items: center !important; width: 100% !important; }
    .q-sender-name { font-size: 1rem !important; margin: 0 !important; }
    .q-bubble { padding: 10px 15px !important; font-size: 1rem !important; max-width: 100% !important; }
    .q-content { font-size: 1rem !important; }
    .q-analysis-note { font-size: 0.8rem !important; transform: none !important; margin-top: 5px !important; }
    .quality-section { padding: 20px 15px !important; }
    .theme-title-badge { font-size: 1.2rem !important; }
    .time-range { font-size: 1rem !important; }
    .speech-bubble { padding: 15px !important; font-size: 1rem !important; border-radius: 20px !important; }
    .character-container { width: 80px !important; height: 80px !important; }
    .dimension-comments-grid { grid-template-columns: 1fr !important; }
    .dim-sticker { font-size: 0.85rem !important; }
    .footer { flex-direction: column !important; gap: 10px !important; padding: 20px 15px !important; }
    .footer-card { padding: 12px !important; }
    .footer-card-title { font-size: 1rem !important; }
    .footer-card-content { font-size: 0.85rem !important; }
}
@media (max-width: 380px) {
    html { font-size: 12px !important; }
    .title-sticker h1 { font-size: 1.2rem !important; }
    .stats-grid { grid-template-columns: 1fr 1fr !important; }
}
</style>'''

# ========== 认证路由 ==========
@app.route('/auth', methods=['POST'])
def auth():
    password = request.form.get('password', '').strip()
    next_url = request.form.get('next', '/')
    device_id = request.form.get('device_id', '').strip() or None
    nickname = request.form.get('nickname', '').strip()
    ip = get_client_ip()

    # 1. 昵称不能为空
    if not nickname:
        return render_template('login.html', error='昵称不能为空，请填写您的昵称', next_url=next_url), 400

    # 2. 检查 IP 是否被封禁
    if is_ip_blocked(ip):
        return render_template('login.html', error='您的 IP 已被临时封禁，请稍后再试', next_url=next_url), 403

    # 3. 验证密码
    if password == app.config['ACCESS_PASSWORD']:
        role = 'user'
    elif password == app.config['ADMIN_PASSWORD']:
        role = 'admin'
    else:
        record_failed_attempt(ip)
        if is_ip_blocked(ip):
            error = '密码错误次数过多，IP 已被封禁 1 小时'
        else:
            error = '密码错误，请重试'
        return render_template('login.html', error=error, next_url=next_url)

    # 4. 密码正确 → 写入/更新白名单（强制 authorized=1）
    authorize_ip(ip, request.headers.get('User-Agent', ''), request.referrer or '/', device_id, nickname)

    # 5. 清除该 IP 的失败记录
    conn = get_db_conn()
    conn.execute('DELETE FROM ip_blocklist WHERE ip=?', (ip,))
    conn.commit()
    conn.close()

    # 6. 设置 session
    session['authorized'] = True
    session['role'] = role
    session['device_id'] = device_id
    session['nickname'] = nickname  # [新增] 保存昵称，供日志使用

    # [新增] 记录登录操作
    log_operation('login', {
        'role': role,
        'device_id': device_id,
        'ip': ip,
        'nickname': nickname
    }, nickname=nickname, ip=ip, device_id=device_id)

    return redirect(next_url)

# ========== 全局请求拦截（认证 + 昵称校验 + 数据库授权校验） ==========
@app.before_request
def check_access():
    # 放行静态资源和认证接口
    if request.path.startswith('/static/') or request.path == '/favicon.ico':
        return
    if request.path == '/auth':
        return

    ip = get_client_ip()
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"[DEBUG] check_access: path={request.path}, ip={ip}, session.authorized={session.get('authorized')}")

    # 1. IP 封禁检查
    if is_ip_blocked(ip):
        print(f"[DEBUG] IP {ip} is blocked")
        if request.path.startswith('/yard/admin/api/'):
            return jsonify({'error': 'IP blocked'}), 403
        session.clear()
        return render_template('login.html', error='您的 IP 已被封禁，请稍后再试', next_url=request.url), 403

    device_id = session.get('device_id')
    db_authorized = False

    # 2. 如果 session 已授权，必须强制校验数据库中 authorized 是否为 1（取最新记录）
    if session.get('authorized'):
        conn = get_db_conn()
        cur = conn.cursor()
        if device_id:
            cur.execute('SELECT authorized FROM access_whitelist WHERE device_id=? AND date=? ORDER BY id DESC LIMIT 1', (device_id, today))
            row = cur.fetchone()
            if row and row[0] == 1:
                db_authorized = True
        if not db_authorized:
            cur.execute('SELECT authorized FROM access_whitelist WHERE ip=? AND date=? ORDER BY id DESC LIMIT 1', (ip, today))
            row = cur.fetchone()
            if row and row[0] == 1:
                db_authorized = True
        conn.close()

        if not db_authorized:
            print(f"[DEBUG] Database authorized=0 for device_id={device_id} or ip={ip}, forcing re-login")
            session.clear()
            return render_template('login.html', error='管理员已要求重新登录，请重新验证', next_url=request.url), 401

        # 校验通过：更新白名单记录（刷新 last_access / IP / UA）
        authorize_ip(ip, request.headers.get('User-Agent', ''), request.path, device_id, None)
        return None

    # 3. 未登录（session 无效）：检查 IP 是否在白名单中（兼容旧逻辑）
    if is_ip_authorized(ip):
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('SELECT nickname, device_id FROM access_whitelist WHERE ip=? AND date=? AND authorized=1 ORDER BY id DESC LIMIT 1', (ip, today))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            # 有昵称且 authorized=1 → 恢复 session
            session['authorized'] = True
            session['role'] = 'user'
            session['nickname'] = row[0]  # [新增] 恢复昵称
            if row[1]:
                session['device_id'] = row[1]
            authorize_ip(ip, request.headers.get('User-Agent', ''), request.path, session.get('device_id'), None)
            return None
        else:
            # 没有昵称或 authorized=0 → 跳登录页
            return render_template('login.html', error='请设置您的昵称以便继续访问', next_url=request.url), 401

    # 4. 完全未授权 → 跳转登录
    print(f"[DEBUG] Unauthorized, redirect to login")
    return render_template('login.html', next_url=request.url), 401

# ========== 原有路由（group / yard / etc. 保持不变） ==========
@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>&#127775; 晚报中心 &#127775;</title>
    <style>
        :root {
            --primary: #ff6b9d;
            --secondary: #c44569;
            --accent: #4ade80;
            --bg-dark: #1a1a2e;
            --bg-card: #16213e;
            --text-main: #eaeaea;
            --text-muted: #a0a0a0;
        }
        body {
            font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
            background: linear-gradient(135deg, var(--bg-dark) 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: var(--text-main);
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0;
        }
        .center-box { text-align: center; max-width: 600px; padding: 40px; }
        h1 {
            font-size: 2.5em;
            background: linear-gradient(45deg, var(--primary), var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 40px;
        }
        .card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }
        .nav-card {
            background: linear-gradient(135deg, var(--bg-card), rgba(22, 33, 62, 0.8));
            border-radius: 20px;
            padding: 40px 30px;
            border: 2px solid rgba(233, 69, 96, 0.3);
            transition: all 0.3s ease;
            cursor: pointer;
            text-decoration: none;
            color: var(--text-main);
            display: block;
        }
        .nav-card:hover { transform: translateY(-5px); box-shadow: 0 10px 30px rgba(233, 69, 96, 0.3); border-color: var(--primary); }
        .nav-card.green { border-color: rgba(74, 222, 128, 0.3); }
        .nav-card.green:hover { box-shadow: 0 10px 30px rgba(74, 222, 128, 0.3); border-color: var(--accent); }
        .card-icon { font-size: 3em; margin-bottom: 15px; }
        .card-title { font-size: 1.5em; font-weight: bold; margin-bottom: 10px; }
        .card-desc { color: var(--text-muted); font-size: 0.9em; }
        @media (max-width: 600px) { .card-grid { grid-template-columns: 1fr; } h1 { font-size: 1.8em; } }
    </style>
</head>
<body>
    <div class="center-box">
        <h1>&#127775; 晚报中心 &#127775;</h1>
        <div class="card-grid">
            <a href="/group" class="nav-card">
                <div class="card-icon">&#128172;</div>
                <div class="card-title" style="color: #ff6b9d;">群聊晚报</div>
                <div class="card-desc">QQ 群聊数据分析日报</div>
            </a>
            <a href="/yard" class="nav-card green">
                <div class="card-icon">&#127807;</div>
                <div class="card-title" style="color: #4ade80;">庭院晚报</div>
                <div class="card-desc">Heiboxyard 庭院晚报</div>
            </a>
        </div>
    </div>
</body>
</html>'''

# ========== 群聊路由 ==========
@app.route('/group')
def group_index():
    items, total_count, total_size = scan_reports(GROUP_REPORTS_DIR, '/group')
    return build_index_page(
        title="群聊日报中心",
        subtitle="QQ 群聊数据报告归档与浏览",
        icon="&#128172;",
        css=ANIME_CSS,
        items=items,
        total_count=total_count,
        total_size=total_size,
        other_title="庭院晚报",
        other_link="/yard",
        other_desc="&#127807; 查看庭院晚报"
    )

@app.route('/group/view/<filename>')
def group_view(filename):
    if not filename.lower().endswith('.html'):
        return 'Forbidden', 403
    safe_path = os.path.normpath(os.path.join(GROUP_REPORTS_DIR, filename))
    if not safe_path.startswith(os.path.normpath(GROUP_REPORTS_DIR)):
        return "Forbidden", 403
    if not os.path.exists(safe_path):
        return "File not found", 404
    with open(safe_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    if '<meta name="viewport"' not in html_content:
        html_content = html_content.replace("<head>", "<head>\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no\">")
    if '</head>' in html_content:
        html_content = html_content.replace("</head>", MOBILE_FIX_CSS + "\n</head>")
    return html_content

@app.route('/group/image/<filename>')
def group_image(filename):
    safe_path = os.path.normpath(os.path.join(GROUP_REPORTS_DIR, filename))
    if not safe_path.startswith(os.path.normpath(GROUP_REPORTS_DIR)):
        return "Forbidden", 403
    if not os.path.exists(safe_path):
        return "File not found", 404
    if not filename.lower().endswith(('.jpg', '.jpeg', '.png')):
        return "Forbidden", 403
    return send_file(safe_path)

# ========== 庭院路由 ==========
@app.route('/yard')
def yard_index():
    items, total_count, total_size = scan_reports(YARD_REPORTS_DIR, '/yard')
    return build_index_page(
        title="庭院日报中心",
        subtitle="Heiboxyard 庭院数据报告归档与浏览",
        icon="&#127807;",
        css=YARD_CSS,
        items=items,
        total_count=total_count,
        total_size=total_size,
        other_title="群聊晚报",
        other_link="/group",
        other_desc="&#128172; 查看群聊晚报"
    )

@app.route('/yard/view/<filename>')
def yard_view(filename):
    if not filename.lower().endswith('.html'):
        return 'Forbidden', 403
    safe_path = os.path.normpath(os.path.join(YARD_REPORTS_DIR, filename))
    if not safe_path.startswith(os.path.normpath(YARD_REPORTS_DIR)):
        return "Forbidden", 403
    if not os.path.exists(safe_path):
        return "File not found", 404
    with open(safe_path, 'r', encoding='utf-8') as f:
        return f.read()

@app.route('/yard/image/<filename>')
def yard_image(filename):
    safe_path = os.path.normpath(os.path.join(YARD_REPORTS_DIR, filename))
    if safe_path.startswith(os.path.normpath(YARD_REPORTS_DIR)) and os.path.exists(safe_path):
        return send_file(safe_path)
    safe_img = os.path.normpath(os.path.join(YARD_IMAGES_DIR, filename))
    if safe_img.startswith(os.path.normpath(YARD_IMAGES_DIR)) and os.path.exists(safe_img):
        return send_file(safe_img)
    return "File not found", 404

@app.route('/yard/images/<filename>')
def yard_images(filename):
    safe_path = os.path.normpath(os.path.join(YARD_IMAGES_DIR, filename))
    if not safe_path.startswith(os.path.normpath(YARD_IMAGES_DIR)):
        return "Forbidden", 403
    if not os.path.exists(safe_path):
        return "File not found", 404
    if not filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
        return "Forbidden", 403
    return send_file(safe_path)

# ========== 旧版兼容路由 ==========
@app.route('/view/<filename>')
def legacy_view(filename):
    return group_view(filename)

@app.route('/image/<filename>')
def legacy_image(filename):
    return group_image(filename)

# ========== 挂载 heiboxyard 管理端 ==========
try:
    from yard_admin import yard_bp
    app.register_blueprint(yard_bp)
    print("✅ heiboxyard Web 管理端已挂载到 /yard/admin")
except ImportError as e:
    print(f"⚠️ heiboxyard Web 管理端挂载失败: {e}")

# ========== 初始化数据库 ==========
init_db()

# ========== MC-Server ==========
# 初始化 mcserver 数据库
mc_monitor.init_db()

# 启动 mcserver 监控线程
mc_monitor.start_monitor()

mc_bp = Blueprint('mcserver', __name__, template_folder='mcserver/templates')

@mc_bp.route('/')
def mcserver_index():
    return render_template('index.html')

@mc_bp.route('/api/latest')
def mcserver_latest():
    data = mc_monitor.get_latest_status()
    return jsonify(data)

@mc_bp.route('/api/daily')
def mcserver_daily():
    data = mc_monitor.get_daily_ratios()
    return jsonify(data)

# 注册蓝图，设置前缀 /mcserver
app.register_blueprint(mc_bp, url_prefix='/mcserver')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
