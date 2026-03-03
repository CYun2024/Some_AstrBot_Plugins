"""
LLM Debugger Plugin for AstrBot
LLM 调用监控调试器（带WebUI）

版本: 1.2.0
作者: 韶虹CYun
"""

from astrbot.api.star import register
from .main import LLMDebugger as _LLMDebugger

LLMDebugger = register(
    name="llm_debugger",
    author="韶虹CYun",
    desc="LLM 调用监控调试器（带WebUI）- 修复版，支持插件主动上报",
    version="1.2.0"
)(_LLMDebugger)

__all__ = ["LLMDebugger"]
