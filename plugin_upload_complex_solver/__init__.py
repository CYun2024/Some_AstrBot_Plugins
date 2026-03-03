from astrbot.api.star import register
from .main import ComplexSolverPlugin as _ComplexSolverPlugin

# 重新注册以确保 AstrBot 能识别元数据
ComplexSolverPlugin = register(
    name="complex_solver",
    author="CYun2024",
    desc="多模型协作求解插件，自动识别复杂问题并调用专业模型，人设化复述解题过程",
    version="2.0.11",  # 与 main.py 保持一致
    repo="https://github.com/CYun2024/astrbot_plugin_complex_solver"
)(_ComplexSolverPlugin)

__all__ = ["ComplexSolverPlugin"]