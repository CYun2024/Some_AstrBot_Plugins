# -*- coding: utf-8 -*-
from flask import Flask, send_file, jsonify, request, session, redirect, render_template
import os
from datetime import datetime, timedelta
import sqlite3
from werkzeug.middleware.proxy_fix import ProxyFix   # 新增

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

# 应用 ProxyFix 中间件，使 Flask 识别代理转发的 Host/Proto
# x_for=1, x_proto=1, x_host=1 表示信任 X-Forwarded-For, X-Forwarded-Proto, X-Forwarded-Host
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
    conn.commit()
    conn.close()

def get_client_ip():
    # 优先从 X-Forwarded-For 获取真实 IP（可能有多层代理）
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        # 取第一个 IP（最原始客户端）
        ip = forwarded.split(',')[0].strip()
    else:
        ip = request.remote_addr
    # 如果获取到的是内部 IP（127.0.0.1 或 10.0.0.x），但 XFF 可能为空，则尝试从 X-Real-IP 获取
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
            # 过期则清除记录
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
    cur.execute('SELECT 1 FROM access_whitelist WHERE ip=? AND date=? AND authorized=1', (ip, today))
    row = cur.fetchone()
    conn.close()
    return row is not None

def authorize_ip(ip, user_agent, path):
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"[DEBUG] authorize_ip called: ip={ip}, today={today}, path={path}")
    conn = get_db_conn()
    cur = conn.cursor()
    # 先检查该IP今天是否有授权记录
    cur.execute('SELECT id, last_access FROM access_whitelist WHERE ip=? AND date=? AND authorized=1', (ip, today))
    row = cur.fetchone()
    if row:
        # 更新
        cur.execute('''
            UPDATE access_whitelist SET last_access=CURRENT_TIMESTAMP, user_agent=?, path=?
            WHERE ip=? AND date=? AND authorized=1
        ''', (user_agent, path, ip, today))
        print(f"[DEBUG] Updated last_access for IP {ip}, old last_access={row[1]}")
    else:
        # 插入
        cur.execute('''
            INSERT INTO access_whitelist (ip, date, user_agent, path)
            VALUES (?, ?, ?, ?)
        ''', (ip, today, user_agent, path))
        print(f"[DEBUG] Inserted new record for IP {ip}")
    conn.commit()
    conn.close()
    print(f"[DEBUG] authorize_ip completed for IP {ip}")

# ========== 文件大小/时间辅助（原有） ==========
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

# ========== CSS（原有） ==========
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
/* === 移动端紧急修复 === */
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

# ========== 认证路由（统一登录） ==========
@app.route('/auth', methods=['POST'])
def auth():
    password = request.form.get('password', '').strip()
    next_url = request.form.get('next', '/')
    ip = get_client_ip()

    # 检查 IP 是否已被封禁
    if is_ip_blocked(ip):
        return render_template('login.html', error='您的 IP 已被临时封禁，请稍后再试', next_url=next_url), 403

    if password == app.config['ACCESS_PASSWORD']:
        # 普通用户
        authorize_ip(ip, request.headers.get('User-Agent', ''), request.referrer or '/')
        session['authorized'] = True
        session['role'] = 'user'
        # 清除该 IP 的失败记录
        conn = get_db_conn()
        conn.execute('DELETE FROM ip_blocklist WHERE ip=?', (ip,))
        conn.commit()
        conn.close()
        return redirect(next_url)
    elif password == app.config['ADMIN_PASSWORD']:
        # 管理员
        authorize_ip(ip, request.headers.get('User-Agent', ''), request.referrer or '/')
        session['authorized'] = True
        session['role'] = 'admin'
        conn = get_db_conn()
        conn.execute('DELETE FROM ip_blocklist WHERE ip=?', (ip,))
        conn.commit()
        conn.close()
        return redirect(next_url)
    else:
        # 密码错误
        record_failed_attempt(ip)
        if is_ip_blocked(ip):
            error = '密码错误次数过多，IP 已被封禁 1 小时'
        else:
            error = '密码错误，请重试'
        return render_template('login.html', error=error, next_url=next_url)

# ========== 全局请求拦截（认证检查 + 封禁检查） ==========
@app.before_request
def check_access():
    # 放行静态资源
    if request.path.startswith('/static/') or request.path == '/favicon.ico':
        return
    # 放行认证路由
    if request.path == '/auth':
        return
    ip = get_client_ip()
    print(f"[DEBUG] check_access: path={request.path}, ip={ip}, session.authorized={session.get('authorized')}")

    # 检查 IP 是否被封禁
    if is_ip_blocked(ip):
        print(f"[DEBUG] IP {ip} is blocked")
        if request.path.startswith('/yard/admin/api/'):
            return jsonify({'error': 'IP blocked'}), 403
        if session.get('authorized'):
            session.clear()
            return render_template('login.html', error='您的 IP 已被封禁，请稍后再试', next_url=request.url), 403
        else:
            return render_template('login.html', error='您的 IP 已被封禁，请稍后再试', next_url=request.url), 403

    # 检查是否已授权（session）
    if session.get('authorized'):
        print(f"[DEBUG] Session authorized, updating last_access for IP {ip}")
        authorize_ip(ip, request.headers.get('User-Agent', ''), request.path)
        return None

    # 检查 IP 是否在白名单中
    if is_ip_authorized(ip):
        print(f"[DEBUG] IP {ip} is in whitelist, setting session and updating last_access")
        session['authorized'] = True
        session['role'] = 'user'
        authorize_ip(ip, request.headers.get('User-Agent', ''), request.path)
        return None

    # 未授权，跳转登录
    print(f"[DEBUG] Unauthorized, redirect to login")
    return render_template('login.html', next_url=request.url), 401
# ========== 原有路由 ==========
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

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
