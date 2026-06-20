"""
HTML模板模块
使用Jinja2加载外部HTML模板文件
"""

import os
import threading

from jinja2 import Environment, FileSystemLoader, select_autoescape

from astrbot.api import logger


class HTMLTemplates:
    """HTML模板管理类"""

    def __init__(self, template_dir: str):
        self.base_dir = template_dir
        self._envs = {}
        self._env_lock = threading.Lock()

    def _get_env(self, theme_name: str = "default") -> Environment:
        with self._env_lock:
            env = self._envs.get(theme_name)
            if env is not None:
                return env

        template_dir = os.path.join(self.base_dir, theme_name)
        if not os.path.exists(template_dir):
            logger.warning(f"模板目录不存在: {template_dir}，回退到 default")
            template_dir = os.path.join(self.base_dir, "default")

        env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        with self._env_lock:
            existing = self._envs.get(theme_name)
            if existing is not None:
                return existing
            self._envs[theme_name] = env

        return env

    def render_template(self, template_name: str, theme_name: str = "default", **kwargs) -> str:
        try:
            env = self._get_env(theme_name)
            template = env.get_template(template_name)
            return template.render(**kwargs)
        except Exception as e:
            logger.error(f"渲染模板 {theme_name}/{template_name} 失败: {e}")
            return ""