"""
Complex Solver Plugin for AstrBot
多模型协作求解插件，支持双模型并行、@提及、人设复述等功能

版本: 3.1.1
作者: CYun2024
仓库: https://github.com/CYun2024/astrbot_plugin_complex_solver
"""

from astrbot.api.star import register
from .main import ComplexSolverPlugin as _ComplexSolverPlugin

ComplexSolverPlugin = register(
    name="complex_solver",
    author="CYun2024",
    desc="多模型协作求解插件，支持双模型并行、@提及、人设复述/精简模型直接用人设输出",
    version="3.1.1",
    repo="https://github.com/CYun2024/astrbot_plugin_complex_solver"
)(_ComplexSolverPlugin)

__all__ = ["ComplexSolverPlugin"]

# 导出子模块（供其他插件使用）
from .debugger_reporter import DebuggerReporter
from .at_handler import AtHandler
from .latex_renderer import LatexRenderer
from .persona import PersonaHandler
from .summarizer import Summarizer
from .solver import Solver
from .question_judge import QuestionJudge
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
    "QuestionJudge",
    "extract_images",
    "build_history_messages",
    "make_serializable",
    "format_timestamp",
    "truncate_text"
])
