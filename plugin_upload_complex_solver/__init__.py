"""
Complex Solver Plugin for AstrBot
意图驱动的多模型协作求解，区分OCR和场景理解

版本: 4.2.0
作者: CYun2024
仓库: https://github.com/CYun2024/astrbot_plugin_complex_solver
"""

from astrbot.api.star import register
from .main import ComplexSolverPlugin as _ComplexSolverPlugin

ComplexSolverPlugin = register(
    name="complex_solver",
    author="CYun2024",
    desc="意图驱动的多模型协作求解，区分OCR和场景理解，支持快速判断模型",
    version="4.2.0",
    repo="https://github.com/CYun2024/astrbot_plugin_complex_solver"
)(_ComplexSolverPlugin)

__all__ = ["ComplexSolverPlugin"]

# 导出子模块
from .debugger_reporter import DebuggerReporter
from .at_handler import AtHandler
from .latex_renderer import LatexRenderer
from .persona import PersonaHandler
from .summarizer import Summarizer
from .solver import Solver
from .intent_judge import IntentJudge  # 更新为新的意图判断
from .utils import (
    extract_images,
    build_history_messages,
    make_serializable,
    format_timestamp,
    truncate_text
)

__all__.extend([
    "DebuggerReporter",
    "AtHandler",
    "LatexRenderer",
    "PersonaHandler",
    "Summarizer",
    "Solver",
    "IntentJudge",  # 更新
    "extract_images",
    "build_history_messages",
    "make_serializable",
    "format_timestamp",
    "truncate_text"
])