# mcserver/monitor.py
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
import os

# 引入 mcstatus 库（需先用 pip install mcstatus 安装）
from mcstatus import JavaServer

# 当前文件所在目录（mcserver）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据库放在上级目录（即 astrbot_reports_server.py 所在目录）以便确保可写
DB_PATH = os.path.join(os.path.dirname(BASE_DIR), "mcserver.db")

# ========== Minecraft MOTD 解析函数 ==========
COLOR_MAP = {
    'black': '#000000', 'dark_blue': '#0000AA', 'dark_green': '#00AA00',
    'dark_aqua': '#00AAAA', 'dark_red': '#AA0000', 'dark_purple': '#AA00AA',
    'gold': '#FFAA00', 'gray': '#AAAAAA', 'dark_gray': '#555555',
    'blue': '#5555FF', 'green': '#55FF55', 'aqua': '#55FFFF',
    'red': '#FF5555', 'light_purple': '#FF55FF', 'yellow': '#FFFF55',
    'white': '#FFFFFF'
}

def parse_motd(description):
    """
    解析 MOTD，返回 (纯文本, HTML格式)
    兼容 mcstatus 返回的 description 对象（可能是字符串或字典）
    """
    if isinstance(description, str):
        return description, description
    if isinstance(description, dict):
        if 'extra' in description and isinstance(description['extra'], list):
            plain_parts = []
            html_parts = []
            for part in description['extra']:
                if 'text' in part:
                    text = part['text']
                    plain_parts.append(text)
                    style_attrs = []
                    if 'color' in part:
                        color = part['color']
                        if color in COLOR_MAP:
                            style_attrs.append(f"color:{COLOR_MAP[color]};")
                        else:
                            style_attrs.append(f"color:{color};")
                    if part.get('bold', False):
                        style_attrs.append("font-weight:bold;")
                    if part.get('italic', False):
                        style_attrs.append("font-style:italic;")
                    if part.get('underlined', False):
                        style_attrs.append("text-decoration:underline;")
                    if part.get('strikethrough', False):
                        style_attrs.append("text-decoration:line-through;")
                    if part.get('obfuscated', False):
                        style_attrs.append("font-family: monospace; letter-spacing: 2px;")
                    if style_attrs:
                        style_str = " ".join(style_attrs)
                        html_parts.append(f'<span style="{style_str}">{text}</span>')
                    else:
                        html_parts.append(text)
            return "".join(plain_parts), "".join(html_parts)
        elif 'text' in description:
            return description['text'], description['text']
    return "No MOTD", "No MOTD"

# ========== 使用 mcstatus 获取服务器信息（兼容旧版本） ==========
def get_server_info(host, port):
    """
    通过 mcstatus 库获取 Minecraft 服务器状态
    注意：旧版本库不支持 timeout 参数，故省略
    """
    try:
        server = JavaServer.lookup(f"{host}:{port}")
        status = server.status()  # 旧版无 timeout 参数

        # 解析 MOTD
        raw_motd = status.description
        plain_motd, html_motd = parse_motd(raw_motd) if raw_motd else ("No MOTD", "No MOTD")

        # 尝试获取玩家列表（非必须，部分服务器可能不支持 query）
        players_list = []
        try:
            query = server.query()  # 旧版也无 timeout 参数
            players_list = query.players.names if query.players else []
        except Exception:
            # query 可能因服务器设置或超时失败，忽略即可
            pass

        return {
            "online": True,
            "host": host,
            "port": port,
            "version": status.version.name if status.version else "Unknown",
            "motd_plain": plain_motd,
            "motd_html": html_motd,
            "players": {
                "online": status.players.online,
                "max": status.players.max,
                "list": players_list
            },
            "ping": status.latency if status.latency else 0
        }
    except Exception as e:
        return {"online": False, "host": host, "port": port, "error": str(e)}

# ========== 数据库操作（不变） ==========
def init_db():
    """初始化数据库，确保表存在。数据库路径位于上级目录，确保可写。"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS server_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            online INTEGER NOT NULL,
            players_online INTEGER,
            players_max INTEGER,
            motd TEXT,
            ping REAL,
            version TEXT
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON server_status(timestamp)')
    conn.commit()
    conn.close()

def get_latest_status():
    """获取最近一条服务器状态记录"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT timestamp, online, players_online, players_max, motd, ping, version
        FROM server_status
        ORDER BY timestamp DESC LIMIT 1
    ''')
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "timestamp": row[0],
            "online": bool(row[1]),
            "players_online": row[2],
            "players_max": row[3],
            "motd": row[4],
            "ping": row[5],
            "version": row[6]
        }
    return {"online": None}

def get_daily_ratios(days=90):
    """返回最近 days 天的每日在线比例（0~1），缺失天数返回 None"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    start_ts = int(start_date.timestamp())
    end_ts = int((end_date + timedelta(days=1)).timestamp())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT
            strftime('%Y-%m-%d', timestamp, 'unixepoch') as day,
            SUM(online) as online_count,
            COUNT(*) as total_count
        FROM server_status
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY day
        ORDER BY day DESC
    ''', (start_ts, end_ts))
    rows = c.fetchall()
    conn.close()

    result = {}
    for day, online_count, total_count in rows:
        ratio = online_count / total_count if total_count else 0
        result[day] = ratio

    # 补全所有日期
    full = {}
    cur = start_date
    end_date_only = end_date.date()
    while cur.date() <= end_date_only:
        key = cur.strftime("%Y-%m-%d")
        full[key] = result.get(key, None)
        cur += timedelta(days=1)
    return full

# ========== 后台监控线程 ==========
SERVER_HOST = "play.simpfun.cn"
SERVER_PORT = 28273
CHECK_INTERVAL = 300  # 5分钟

def check_and_record():
    """监控循环，每 5 分钟采集一次并写入数据库"""
    while True:
        try:
            info = get_server_info(SERVER_HOST, SERVER_PORT)
            now = int(time.time())
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''
                INSERT INTO server_status
                (timestamp, online, players_online, players_max, motd, ping, version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                now,
                1 if info.get("online") else 0,
                info.get("players", {}).get("online", 0),
                info.get("players", {}).get("max", 0),
                info.get("motd_plain", ""),
                info.get("ping", -1),
                info.get("version", "")
            ))
            conn.commit()
            conn.close()
            print(f"[MCServer] {datetime.now()} 记录: {'在线' if info.get('online') else '离线'}")
        except Exception as e:
            print(f"[MCServer] 监控错误: {e}")
        time.sleep(CHECK_INTERVAL)

def start_monitor():
    """启动后台监控线程（守护线程）"""
    thread = threading.Thread(target=check_and_record, daemon=True)
    thread.start()
