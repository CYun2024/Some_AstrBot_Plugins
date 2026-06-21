"""
小黑盒插件工具模块
提供通用工具函数、时间窗口计算等
"""
import re
from datetime import datetime, timezone, timedelta


def clean_html_tags(text: str) -> str:
    """清除 HTML 标签"""
    if not text:
        return text
    cleaned = re.sub(r'<[^>]+>', ' ', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def get_today_window() -> tuple[int, int]:
    """
    获取今日时间窗口（北京时间 22:00 为界）
    返回 (window_start, window_end) UTC 时间戳
    """
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
    """
    获取分析窗口（昨日 22:00 ~ 今日 22:00）
    返回 (window_start, window_end) UTC 时间戳
    """
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    today_22 = now_bj.replace(hour=22, minute=0, second=0, microsecond=0)
    window_start = today_22 - timedelta(days=1)
    window_end = today_22
    return (
        int(window_start.astimezone(timezone.utc).timestamp()),
        int(window_end.astimezone(timezone.utc).timestamp())
    )


def ts_to_bj_str(timestamp: int) -> str:
    """UTC 时间戳转北京时间字符串"""
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_time_str(time_str: str) -> tuple[int, int]:
    """解析 HH:MM 格式时间为 (hour, minute)"""
    try:
        hour, minute = map(int, time_str.split(":"))
        return hour, minute
    except (ValueError, AttributeError):
        return 22, 0


def get_next_target_time(hour: int, minute: int) -> datetime:
    """获取下一个目标时间点（北京时间）"""
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    target = now_bj.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_bj:
        target = target + timedelta(days=1)
    return target


# ==================== 新编号系统工具函数 ====================

def get_date_str_from_ts(timestamp: int) -> str:
    """UTC 时间戳转日期字符串 YYYYMMDD（按北京时间）"""
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y%m%d")


def get_date_str_from_window(window_start: int) -> str:
    """从窗口起始时间获取日期字符串"""
    return get_date_str_from_ts(window_start)


def get_today_date_str() -> str:
    """获取今天日期字符串 YYYYMMDD（北京时间）"""
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    return now_bj.strftime("%Y%m%d")


def get_window_for_date(date_str: str) -> tuple[int, int]:
    """
    根据日期字符串获取对应的时间窗口
    例如 "20260620" -> 2026-06-20 22:00 ~ 2026-06-21 22:00 的 UTC 时间戳
    """
    dt = datetime.strptime(date_str, "%Y%m%d")
    dt = dt.replace(hour=22, minute=0, second=0, microsecond=0,
                    tzinfo=timezone(timedelta(hours=8)))
    window_start = int(dt.astimezone(timezone.utc).timestamp())
    window_end = window_start + 24 * 3600
    return window_start, window_end


def parse_daily_no(daily_no_str: str) -> tuple[str, int]:
    """
    解析编号字符串，返回 (date_str, seq_no)
    例如 "20260620-15" -> ("20260620", 15)
    """
    match = re.match(r'^(\d{8})-(\d+)$', daily_no_str)
    if match:
        return match.group(1), int(match.group(2))
    # 兼容旧版纯数字编号
    return "", int(daily_no_str) if daily_no_str.isdigit() else 0


def format_daily_no(date_str: str, seq_no: int) -> str:
    """格式化编号字符串"""
    return f"{date_str}-{seq_no:02d}"


def get_window_for_timestamp(timestamp: int) -> tuple[int, int]:
    """
    根据时间戳获取对应的 22:00 时间窗口
    例如 2026-06-20 21:00 -> 2026-06-19 22:00 ~ 2026-06-20 22:00
         2026-06-20 23:00 -> 2026-06-20 22:00 ~ 2026-06-21 22:00
    返回 (window_start, window_end) UTC 时间戳
    """
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    day_22 = dt.replace(hour=22, minute=0, second=0, microsecond=0)
    if dt.hour >= 22:
        window_start = day_22
        window_end = day_22 + timedelta(days=1)
    else:
        window_start = day_22 - timedelta(days=1)
        window_end = day_22
    return (
        int(window_start.astimezone(timezone.utc).timestamp()),
        int(window_end.astimezone(timezone.utc).timestamp())
    )


def get_today_daily_no_prefix() -> str:
    """获取今日 daily_no 的日期前缀（如 '20260620'）

    注意：编号日期对应的是窗口结束日期的日期
    即 20260620 编号的帖子 = 2026-06-19 22:00 ~ 2026-06-20 22:00 期间发布的帖子
    """
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    return now_bj.strftime("%Y%m%d")


def get_window_start_from_daily_prefix(prefix: str) -> int:
    """根据 daily_no 前缀（如 '20260620'）计算对应的 window_start

    编号 20260620 的帖子 = 窗口 20260619 22:00 ~ 20260620 22:00
    所以 window_start = 20260619 22:00 的 UTC 时间戳
    """
    dt = datetime.strptime(prefix, "%Y%m%d")
    window_start = dt - timedelta(days=1)
    window_start = window_start.replace(hour=22, minute=0, second=0, microsecond=0,
                                        tzinfo=timezone(timedelta(hours=8)))
    return int(window_start.astimezone(timezone.utc).timestamp())