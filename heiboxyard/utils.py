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


# ==================== 核心窗口编号系统 ====================
#
# 窗口定义：北京时间 22:00 为界
#   窗口编号 = 窗口结束日的 YYYYMMDD
#   例如：2026-06-20 22:00 ~ 2026-06-21 22:00 的窗口编号为 20260621
#
# 关键函数：
#   - get_current_window_no()     -> 获取当前窗口编号（如 "20260621"）
#   - get_window_by_no(no)        -> 根据编号获取窗口起止时间戳
#   - get_window_for_timestamp(ts) -> 根据时间戳获取窗口起止时间戳
#


def get_window_for_timestamp(timestamp: int) -> tuple[int, int]:
    """
    根据时间戳获取对应的 22:00 时间窗口
    窗口编号 = 窗口结束日的 YYYYMMDD
    
    例如 2026-06-20 21:00 -> 窗口 2026-06-19 22:00 ~ 2026-06-20 22:00，编号 20260620
         2026-06-20 23:00 -> 窗口 2026-06-20 22:00 ~ 2026-06-21 22:00，编号 20260621
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


def get_current_window_no() -> str:
    """
    获取当前窗口编号（如 "20260621"）
    窗口编号 = 当前所在窗口的结束日期的 YYYYMMDD
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    _, window_end = get_window_for_timestamp(now_ts)
    return get_date_str_from_ts(window_end)


def get_current_window() -> tuple[int, int]:
    """
    获取当前时间窗口（北京时间 22:00 为界）
    返回 (window_start, window_end) UTC 时间戳
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return get_window_for_timestamp(now_ts)


def get_window_by_no(window_no: str) -> tuple[int, int]:
    """
    根据窗口编号（如 "20260621"）获取窗口起止时间戳
    编号 20260621 = 2026-06-20 22:00 ~ 2026-06-21 22:00
    返回 (window_start, window_end) UTC 时间戳
    """
    dt = datetime.strptime(window_no, "%Y%m%d")
    window_end = dt.replace(hour=22, minute=0, second=0, microsecond=0,
                           tzinfo=timezone(timedelta(hours=8)))
    window_start = window_end - timedelta(days=1)
    return (
        int(window_start.astimezone(timezone.utc).timestamp()),
        int(window_end.astimezone(timezone.utc).timestamp())
    )


def get_window_no_from_start(window_start: int) -> str:
    """
    根据窗口起始时间戳反推窗口编号
    窗口编号 = 窗口结束日的 YYYYMMDD = 窗口起始日+1 的 YYYYMMDD
    """
    dt_start = datetime.fromtimestamp(window_start, tz=timezone(timedelta(hours=8)))
    window_end = dt_start + timedelta(days=1)
    return window_end.strftime("%Y%m%d")


# ==================== 日期/时间工具 ====================

def ts_to_bj_str(timestamp: int) -> str:
    """UTC 时间戳转北京时间字符串"""
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_date_str_from_ts(timestamp: int) -> str:
    """UTC 时间戳转日期字符串 YYYYMMDD（按北京时间）"""
    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y%m%d")


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


# ==================== 编号系统工具 ====================

def parse_daily_no(daily_no_str: str) -> tuple[str, int]:
    """
    解析编号字符串，返回 (window_no, seq_no)
    例如 "20260620-15" -> ("20260620", 15)
    """
    match = re.match(r'^(\d{8})-(\d+)$', daily_no_str)
    if match:
        return match.group(1), int(match.group(2))
    # 兼容旧版纯数字编号
    return "", int(daily_no_str) if daily_no_str.isdigit() else 0


def format_daily_no(window_no: str, seq_no: int) -> str:
    """格式化编号字符串"""
    return f"{window_no}-{seq_no:02d}"


# ==================== 兼容旧函数（标记为废弃，逐步替换） ====================

def get_today_window() -> tuple[int, int]:
    """
    【兼容旧代码】获取当前时间窗口
    等同于 get_current_window()
    """
    return get_current_window()


def get_analysis_window() -> tuple[int, int]:
    """
    【兼容旧代码】获取分析窗口
    按需求：/分析今日帖子 等指令的"今日"指当前窗口编号对应的窗口
    所以等同于 get_current_window()
    """
    return get_current_window()


def get_date_str_from_window(window_start: int) -> str:
    """
    【兼容旧代码】从窗口起始时间获取日期字符串
    注意：返回的是窗口起始日的日期，不是窗口编号！
    如需窗口编号，请使用 get_window_no_from_start()
    """
    return get_date_str_from_ts(window_start)


def get_today_date_str() -> str:
    """【兼容旧代码】获取今天日期字符串 YYYYMMDD（北京时间）"""
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    return now_bj.strftime("%Y%m%d")


def get_today_daily_no_prefix() -> str:
    """
    【兼容旧代码，但已修复】获取当前窗口编号
    原实现返回"今天"的日期，22:00 后会出错
    现改为返回当前窗口编号（窗口结束日）
    """
    return get_current_window_no()


def get_window_start_from_daily_prefix(prefix: str) -> int:
    """
    【兼容旧代码】根据 daily_no 前缀（如 '20260620'）计算对应的 window_start
    编号 20260620 的帖子 = 窗口 20260619 22:00 ~ 20260620 22:00
    所以 window_start = 20260619 22:00 的 UTC 时间戳
    """
    dt = datetime.strptime(prefix, "%Y%m%d")
    window_start = dt - timedelta(days=1)
    window_start = window_start.replace(hour=22, minute=0, second=0, microsecond=0,
                                        tzinfo=timezone(timedelta(hours=8)))
    return int(window_start.astimezone(timezone.utc).timestamp())


def get_window_for_date(date_str: str) -> tuple[int, int]:
    """
    【兼容旧代码】根据日期字符串获取对应的时间窗口
    例如 "20260620" -> 窗口 2026-06-19 22:00 ~ 2026-06-20 22:00
    等同于 get_window_by_no(date_str)
    """
    return get_window_by_no(date_str)