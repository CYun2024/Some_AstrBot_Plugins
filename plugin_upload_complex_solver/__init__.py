from astrbot.api.star import register
from .main import ComplexSolverPlugin as _ComplexSolverPlugin

ComplexSolverPlugin = register(
    name="complex_solver",
    author="CYun2024",
    desc="多模型协作求解插件，支持双模型并行调用、超时重试机制、独立精简模型",
    version="3.0.0",
    repo="https://github.com/CYun2024/astrbot_plugin_complex_solver"
)(_ComplexSolverPlugin)

__all__ = ["ComplexSolverPlugin"]