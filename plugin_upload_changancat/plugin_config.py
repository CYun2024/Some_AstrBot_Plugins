"""插件配置解析模块"""

from dataclasses import dataclass, field
from typing import Any, List


def _to_bool(value: Any, default: bool) -> bool:
    """转换为布尔值"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_int(value: Any, default: int, min_val: int = None, max_val: int = None) -> int:
    """转换为整数"""
    try:
        result = int(value)
        if min_val is not None:
            result = max(min_val, result)
        if max_val is not None:
            result = min(max_val, result)
        return result
    except (TypeError, ValueError):
        return default


def _to_str(value: Any, default: str) -> str:
    """转换为字符串"""
    if value is None:
        return default
    return str(value).strip()


def _parse_list(value: Any) -> List[str]:
    """解析逗号分隔的列表"""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(frozen=True)
class CoreSettings:
    """核心设置"""
    enable: bool = True
    bot_qq_id: str = ""
    daily_report_hour: int = 0
    daily_report_minute: int = 5
    target_groups: List[str] = field(default_factory=list)  # 新增：指定发送每日报告的群号列表


@dataclass(frozen=True)
class RepeatSettings:
    """复读设置"""
    enable: bool = True
    check_message_count: int = 10
    repeat_threshold: int = 5


@dataclass(frozen=True)
class StatsSettings:
    """统计设置"""
    enable_meme_stats: bool = True
    enable_haqi_stats: bool = True
    top_meme_count: int = 3


@dataclass(frozen=True)
class DatabaseSettings:
    """数据库设置"""
    data_retention_days: int = 7


@dataclass(frozen=True)
class PluginConfig:
    """插件总配置"""
    core: CoreSettings = field(default_factory=CoreSettings)
    repeat: RepeatSettings = field(default_factory=RepeatSettings)
    stats: StatsSettings = field(default_factory=StatsSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)


def parse_plugin_config(raw: dict[str, Any] | None) -> PluginConfig:
    """解析插件配置"""
    raw = raw or {}

    # 核心设置
    core_raw = raw.get("core_settings", {})
    core = CoreSettings(
        enable=_to_bool(core_raw.get("enable"), True),
        bot_qq_id=_to_str(core_raw.get("bot_qq_id"), ""),
        daily_report_hour=_to_int(core_raw.get("daily_report_hour"), 0, 0, 23),
        daily_report_minute=_to_int(core_raw.get("daily_report_minute"), 5, 0, 59),
        target_groups=_parse_list(core_raw.get("target_groups", "")),  # 新增：解析群号列表
    )

    # 复读设置
    repeat_raw = raw.get("repeat_settings", {})
    repeat = RepeatSettings(
        enable=_to_bool(repeat_raw.get("enable"), True),
        check_message_count=_to_int(repeat_raw.get("check_message_count"), 10, 3, 50),
        repeat_threshold=_to_int(repeat_raw.get("repeat_threshold"), 5, 2, 20),
    )

    # 统计设置
    stats_raw = raw.get("stats_settings", {})
    stats = StatsSettings(
        enable_meme_stats=_to_bool(stats_raw.get("enable_meme_stats"), True),
        enable_haqi_stats=_to_bool(stats_raw.get("enable_haqi_stats"), True),
        top_meme_count=_to_int(stats_raw.get("top_meme_count"), 3, 1, 10),
    )

    # 数据库设置
    database_raw = raw.get("database_settings", {})
    database = DatabaseSettings(
        data_retention_days=_to_int(database_raw.get("data_retention_days"), 7, 1, 30),
    )

    return PluginConfig(
        core=core,
        repeat=repeat,
        stats=stats,
        database=database,
    )