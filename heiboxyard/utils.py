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