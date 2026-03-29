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


def _to_float(value: Any, default: float, min_val: float = None) -> float:
    """转换为浮点数"""
    try:
        result = float(value)
        if min_val is not None:
            result = max(min_val, result)
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
    trigger_words: List[str] = field(default_factory=list)
    admin_user_id: str = ""
    bot_name: str = "机巧猫"
    bot_qq_id: str = ""
    debug: bool = False  # 新增：调试日志开关


@dataclass(frozen=True)
class ModelSettings:
    """模型配置（主LLM通过上下文注入，无需配置提供商）"""
    model_a_provider: str = ""
    model_a_fallback_provider: str = ""
    model_b_provider: str = ""
    model_b_fallback_provider: str = ""
    vision_provider: str = ""
    vision_prompt: str = "请用中文详细描述这张图片的内容。"


@dataclass(frozen=True)
class ContextSettings:
    """上下文配置"""
    max_context_messages: int = 100
    model_a_context_messages: int = 150
    summary_interval: int = 10
    context_max_age_days: int = 7


@dataclass(frozen=True)
class ActiveReplySettings:
    """主动回复配置（开发中，默认关闭）"""
    enable: bool = False  # 默认关闭，功能开发中
    trigger_keyword: str = "[ACTIVE_REPLY]"
    strict_mode: bool = True
    avoid_controversial: bool = True


@dataclass(frozen=True)
class UserProfileSettings:
    """用户画像配置"""
    enable: bool = True
    daily_update_hour: int = 6
    max_daily_messages: int = 500
    nickname_check_groups: int = 20
    messages_per_group: int = 5


@dataclass(frozen=True)
class ImageCacheSettings:
    """图片缓存配置"""
    enable: bool = True
    max_cache_size: int = 1000  # 最大缓存图片数量
    enable_vision_cache: bool = True  # 是否缓存识图结果


@dataclass(frozen=True)
class TimeoutSettings:
    """超时设置"""
    vision_sec: float = 30.0
    model_a_sec: float = 45.0
    model_b_sec: float = 60.0
    main_llm_sec: float = 60.0


@dataclass(frozen=True)
class PluginConfig:
    """插件总配置"""
    core: CoreSettings = field(default_factory=CoreSettings)
    models: ModelSettings = field(default_factory=ModelSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    active_reply: ActiveReplySettings = field(default_factory=ActiveReplySettings)
    user_profile: UserProfileSettings = field(default_factory=UserProfileSettings)
    image_cache: ImageCacheSettings = field(default_factory=ImageCacheSettings)
    timeouts: TimeoutSettings = field(default_factory=TimeoutSettings)


def parse_plugin_config(raw: dict[str, Any] | None) -> PluginConfig:
    """解析插件配置"""
    raw = raw or {}

    # 核心设置
    core_raw = raw.get("core_settings", {})
    core = CoreSettings(
        enable=_to_bool(core_raw.get("enable"), True),
        trigger_words=_parse_list(core_raw.get("trigger_words", "bot,机器人,机巧猫")),
        admin_user_id=_to_str(core_raw.get("admin_user_id"), ""),
        bot_name=_to_str(core_raw.get("bot_name"), "机巧猫"),
        bot_qq_id=_to_str(core_raw.get("bot_qq_id"), ""),
        debug=_to_bool(core_raw.get("debug"), False),
    )

    # 模型配置（已移除 main_llm_provider）
    models_raw = raw.get("model_settings", {})
    models = ModelSettings(
        model_a_provider=_to_str(models_raw.get("model_a_provider"), ""),
        model_a_fallback_provider=_to_str(models_raw.get("model_a_fallback_provider"), ""),
        model_b_provider=_to_str(models_raw.get("model_b_provider"), ""),
        model_b_fallback_provider=_to_str(models_raw.get("model_b_fallback_provider"), ""),
        vision_provider=_to_str(models_raw.get("vision_provider"), ""),
        vision_prompt=_to_str(
            models_raw.get("vision_prompt"), 
            "请用中文详细描述这张图片的内容。"
        ),
    )

    # 上下文配置
    context_raw = raw.get("context_settings", {})
    context = ContextSettings(
        max_context_messages=_to_int(context_raw.get("max_context_messages"), 100, 10, 500),
        model_a_context_messages=_to_int(context_raw.get("model_a_context_messages"), 150, 10, 1000),
        summary_interval=_to_int(context_raw.get("summary_interval"), 10, 1, 100),
        context_max_age_days=_to_int(context_raw.get("context_max_age_days"), 7, 1, 30),
    )

    # 主动回复配置（开发中，默认关闭）
    active_reply_raw = raw.get("active_reply_settings", {})
    active_reply = ActiveReplySettings(
        enable=_to_bool(active_reply_raw.get("enable"), False),  # 默认关闭
        trigger_keyword=_to_str(active_reply_raw.get("trigger_keyword"), "[ACTIVE_REPLY]"),
        strict_mode=_to_bool(active_reply_raw.get("strict_mode"), True),
        avoid_controversial=_to_bool(active_reply_raw.get("avoid_controversial"), True),
    )

    # 用户画像配置
    user_profile_raw = raw.get("user_profile_settings", {})
    user_profile = UserProfileSettings(
        enable=_to_bool(user_profile_raw.get("enable"), True),
        daily_update_hour=_to_int(user_profile_raw.get("daily_update_hour"), 6, 0, 23),
        max_daily_messages=_to_int(user_profile_raw.get("max_daily_messages"), 500, 100, 2000),
        nickname_check_groups=_to_int(user_profile_raw.get("nickname_check_groups"), 20, 5, 50),
        messages_per_group=_to_int(user_profile_raw.get("messages_per_group"), 5, 1, 20),
    )

    # 图片缓存配置
    image_cache_raw = raw.get("image_cache_settings", {})
    image_cache = ImageCacheSettings(
        enable=_to_bool(image_cache_raw.get("enable"), True),
        max_cache_size=_to_int(image_cache_raw.get("max_cache_size"), 1000, 100, 5000),
        enable_vision_cache=_to_bool(image_cache_raw.get("enable_vision_cache"), True),
    )

    # 超时设置
    timeouts_raw = raw.get("timeouts", {})
    timeouts = TimeoutSettings(
        vision_sec=_to_float(timeouts_raw.get("vision_sec"), 30.0, 1.0),
        model_a_sec=_to_float(timeouts_raw.get("model_a_sec"), 45.0, 1.0),
        model_b_sec=_to_float(timeouts_raw.get("model_b_sec"), 60.0, 1.0),
        main_llm_sec=_to_float(timeouts_raw.get("main_llm_sec"), 60.0, 1.0),
    )

    return PluginConfig(
        core=core,
        models=models,
        context=context,
        active_reply=active_reply,
        user_profile=user_profile,
        image_cache=image_cache,
        timeouts=timeouts,
    )