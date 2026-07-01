"""
晚报报告生成器（重构版）
负责将帖子数据 + AI评论 组装成 HTML 晚报，并支持转图片
增加热评显示，只显示点赞数>=阈值的评论，显示用户名、头像、时间，不显示点赞数
"""
import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

import aiohttp
from astrbot.api import logger

from .templates import HTMLTemplates


class EveningReportGenerator:
    """晚报生成器"""

    def __init__(self, template_dir: str, data_dir: str):
        self.html_templates = HTMLTemplates(template_dir)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.reports_dir = self.data_dir / "reports"
        self.reports_dir.mkdir(exist_ok=True)
        # 头像缓存目录
        self.avatar_cache_dir = self.data_dir / "avatar_cache"
        self.avatar_cache_dir.mkdir(exist_ok=True)

    # ================================================================
    # 主入口：生成完整晚报 HTML
    # ================================================================

    def calculate_issue_no(self, window_no: str) -> int:
        """
        计算期号：以 20260622 为第一期，每天一期

        Args:
            window_no: 窗口编号，格式 YYYYMMDD

        Returns:
            期号（从1开始）
        """
        from datetime import datetime
        base_date = datetime.strptime("20260622", "%Y%m%d")
        try:
            current_date = datetime.strptime(window_no, "%Y%m%d")
        except ValueError:
            return 1
        delta_days = (current_date - base_date).days
        return max(1, delta_days + 1)

    def generate_evening_report(
        self,
        posts: list[dict],
        issue_no: int,
        report_date: str,
        community_name: str = "庭院社区",
        theme: str = "default",
        ai_summary: str = None,
        total_comments: int = 0,
        generation_time: str = None,
        tokens_used: str = None,
        model_used: str = None,
    ) -> str:
        """
        生成晚报 HTML

        Args:
            posts: 帖子列表，每个帖子应包含 hot_comments 字段（热评列表）
            issue_no: 期号
            report_date: 报告日期
            community_name: 社区名称
            theme: 主题
            ai_summary: AI总评价
            total_comments: AI评论总数
            generation_time: 生成时间（日期时间字符串，如 2026-06-24 19:49:00）
            tokens_used: 消耗tokens（格式: 输入/缓存命中/输出）
            model_used: 使用的模型
        """
        total_posts = len(posts)
        total_images = sum(
            len(p.get("image_paths", [])) if isinstance(p.get("image_paths"), list)
            else len(json.loads(p.get("image_paths", "[]")) if p.get("image_paths") else [])
            for p in posts
        )

        posts_html = self._render_posts_list(posts, theme)

        # 生成时间默认为当前时间
        if generation_time is None:
            generation_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        render_data = {
            "issue_no": issue_no,
            "report_date": report_date,
            "community_name": community_name,
            "total_posts": total_posts,
            "total_images": total_images,
            "total_comments": total_comments,
            "posts_html": posts_html,
            "ai_summary": ai_summary or "暂无总评",
            "generation_time": generation_time,
            "tokens_used": tokens_used or "--",
            "model_used": model_used or "--",
        }

        html_content = self.html_templates.render_template(
            "evening_report.html", theme=theme, **render_data
        )

        return html_content

    # ================================================================
    # 子组件渲染
    # ================================================================

    def _render_posts_list(self, posts: list[dict], theme: str) -> str:
        DETAIL_COUNT = 3

        detailed_posts = []
        brief_posts = []

        for i, post in enumerate(posts):
            post_data = self._prepare_post_data(post)
            if i < DETAIL_COUNT:
                detailed_posts.append(post_data)
            else:
                brief_posts.append(post_data)

        detailed_html = self.html_templates.render_template(
            "post_detail.html", theme=theme, posts=detailed_posts
        )

        brief_html = self.html_templates.render_template(
            "post_brief.html", theme=theme, posts=brief_posts
        )

        return detailed_html + "\n" + brief_html

    def _prepare_post_data(self, post: dict) -> dict:
        avatar = post.get("avatar", "")
        avatar_data = self._resolve_avatar(avatar)

        image_data_list = []
        image_paths = post.get("image_paths", [])
        if isinstance(image_paths, str):
            try:
                image_paths = json.loads(image_paths)
            except:
                image_paths = []

        # 限制详情帖子最多展示5张图片
        for img_path in image_paths[:5]:
            img_data = self._image_to_base64(img_path)
            if img_data:
                image_data_list.append(img_data)

        comment = post.get("comment", "") or "暂无评论"

        # 处理热评
        hot_comments = post.get("hot_comments", [])
        hot_comments_html = self._render_hot_comments(hot_comments)

        daily_no = post.get("daily_no", 0)
        daily_no_str = str(daily_no) if daily_no else "0"

        return {
            "daily_no": daily_no_str,
            "title": post.get("title", "(无标题)"),
            "username": post.get("username", "未知用户"),
            "avatar_data": avatar_data,
            "create_at_str": post.get("create_at_str", ""),
            "comment": comment,
            "image_data_list": image_data_list,
            "image_count": len(image_data_list),
            "hot_comments_html": hot_comments_html,   # 新增
        }

    def _render_hot_comments(self, hot_comments: list[dict]) -> str:
        """生成热评 HTML 片段"""
        if not hot_comments:
            return ""

        lines = ['<div class="hot-comments">']
        lines.append('<div class="hot-label">热评喵</div>')
        for hc in hot_comments:
            username = hc.get('username', '匿名')
            text = hc.get('text', '')
            avatar_url = hc.get('avatar', '')
            avatar_data = self._resolve_avatar(avatar_url) if avatar_url else ''
            time_str = hc.get('time_str', '')  # 已格式化

            # 头像
            avatar_img = f'<img src="{avatar_data}" class="hot-avatar" alt="{username}">' if avatar_data else ''
            # 时间
            time_html = f'<span class="hot-time">{time_str}</span>' if time_str else ''

            lines.append(f'''
            <div class="hot-comment">
                {avatar_img}
                <div class="hot-body">
                    <span class="hot-username">{username}</span>
                    {time_html}
                    <div class="hot-text">{text}</div>
                </div>
            </div>
            ''')
        lines.append('</div>')
        return "\n".join(lines)

    # ================================================================
    # 头像处理（修复版：支持下载网络头像）
    # ================================================================

    def _resolve_avatar(self, avatar: str) -> str:
        """解析头像，支持本地路径、HTTP URL，失败返回默认头像"""
        if not avatar:
            return self._get_default_avatar()

        if os.path.exists(avatar):
            return self._image_to_base64(avatar) or self._get_default_avatar()

        if avatar.startswith("http"):
            cached = self._get_cached_avatar(avatar)
            if cached:
                return cached
            try:
                downloaded = self._download_avatar_sync(avatar)
                if downloaded:
                    return downloaded
            except Exception as e:
                logger.warning(f"下载头像失败 {avatar}: {e}")
            return self._get_default_avatar()

        return self._get_default_avatar()

    def _get_cached_avatar(self, url: str) -> Optional[str]:
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_path = self.avatar_cache_dir / f"{url_hash}.txt"
        if cache_path.exists():
            try:
                return cache_path.read_text(encoding="utf-8")
            except Exception:
                pass
        return None

    def _save_avatar_cache(self, url: str, base64_data: str):
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_path = self.avatar_cache_dir / f"{url_hash}.txt"
        try:
            cache_path.write_text(base64_data, encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存头像缓存失败: {e}")

    def _download_avatar_sync(self, url: str) -> Optional[str]:
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                if not data:
                    return None

                mime = "image/jpeg"
                if data.startswith(b"\x89PNG"):
                    mime = "image/png"
                elif data.startswith(b"GIF8"):
                    mime = "image/gif"
                elif data.startswith(b"RIFF"):
                    mime = "image/webp"

                b64 = base64.b64encode(data).decode("utf-8")
                result = f"data:{mime};base64,{b64}"
                self._save_avatar_cache(url, result)
                return result
        except Exception as e:
            logger.warning(f"同步下载头像失败 {url}: {e}")
            return None

    async def _download_avatar_async(self, url: str) -> Optional[str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                       headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.read()
                    if not data:
                        return None

                    mime = "image/jpeg"
                    if data.startswith(b"\x89PNG"):
                        mime = "image/png"
                    elif data.startswith(b"GIF8"):
                        mime = "image/gif"
                    elif data.startswith(b"RIFF"):
                        mime = "image/webp"

                    b64 = base64.b64encode(data).decode("utf-8")
                    result = f"data:{mime};base64,{b64}"
                    self._save_avatar_cache(url, result)
                    return result
        except Exception as e:
            logger.warning(f"异步下载头像失败 {url}: {e}")
            return None

    # ================================================================
    # T2I 图片渲染
    # ================================================================

    async def render_html_to_image(
        self,
        html_content: str,
        html_render_func: Callable,
        image_options: dict = None,
    ) -> Optional[str]:
        """
        将 HTML 渲染为图片，返回 base64:// 或文件路径
        """
        if not html_content:
            logger.error("HTML 内容为空，无法渲染图片")
            return None

        if image_options is None:
            image_options = {"type": "png", "quality": "ultra"}

        try:
            logger.info(f"开始 T2I 渲染，HTML 长度: {len(html_content)} 字符")

            image_data = await html_render_func(
                html_content,
                {},
                False,
                image_options,
            )

            if not image_data:
                logger.error("T2I 渲染返回空数据")
                return None

            is_valid = False
            actual_data = None

            if isinstance(image_data, bytes):
                actual_data = image_data
                data_head = image_data[:10]
            elif isinstance(image_data, str) and os.path.exists(image_data):
                with open(image_data, "rb") as f:
                    actual_data = f.read()
                data_head = actual_data[:10]
            else:
                logger.warning(f"T2I 返回了非预期的数据类型: {type(image_data)}")
                return None

            if data_head.startswith(b"\xff\xd8"):
                is_valid = True
            elif data_head.startswith(b"\x89PNG"):
                is_valid = True
            elif data_head.startswith(b"GIF8"):
                is_valid = True
            elif data_head.startswith(b"RIFF") and b"WEBP" in data_head[:16]:
                is_valid = True

            if not is_valid:
                html_error = None
                try:
                    html_text = actual_data[:4096].decode("utf-8", errors="ignore")
                    if "<html" in html_text.lower():
                        title_match = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
                        if title_match:
                            html_error = title_match.group(1).strip()
                        else:
                            html_error = html_text[:100]
                        logger.warning(f"T2I 返回了错误页面: {html_error}")
                except Exception:
                    pass
                logger.error(f"渲染结果不是有效图片 (头部: {data_head[:4].hex()})")
                return None

            if isinstance(image_data, bytes):
                b64 = base64.b64encode(image_data).decode("utf-8")
                image_url = f"base64://{b64}"
                logger.info(f"图片渲染成功: [Base64 {len(image_data)} bytes]")
                return image_url
            elif isinstance(image_data, str):
                logger.info(f"图片渲染成功: {image_data}")
                return image_data

        except Exception as e:
            logger.error(f"T2I 渲染失败: {e}", exc_info=True)
            return None

    # ================================================================
    # 保存功能
    # ================================================================

    def save_report(self, html_content: str, window_no: str = None, filename: str = None) -> str:
        if not filename:
            if window_no:
                filename = f"evening_report_{window_no}.html"
            else:
                filename = f"evening_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

        # 确保 reports 目录存在（兼容外部传入的 data_dir 可能变化的情况）
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.reports_dir / filename
        file_path.write_text(html_content, encoding="utf-8")
        logger.info(f"晚报 HTML 已保存: {file_path}")
        return str(file_path)

    def save_image(self, image_data: bytes, window_no: str = None, filename: str = None) -> str:
        """保存图片 bytes 到文件"""
        if not filename:
            if window_no:
                filename = f"evening_report_{window_no}.png"
            else:
                filename = f"evening_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

        # 确保 reports 目录存在
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.reports_dir / filename
        file_path.write_bytes(image_data)
        logger.info(f"晚报图片已保存: {file_path}")
        return str(file_path)

    # ================================================================
    # 工具方法
    # ================================================================

    def _image_to_base64(self, image_path: str) -> Optional[str]:
        try:
            path = Path(image_path)
            if not path.exists():
                return None

            with open(path, "rb") as f:
                data = f.read()

            mime = "image/jpeg"
            if data.startswith(b"\x89PNG"):
                mime = "image/png"
            elif data.startswith(b"GIF8"):
                mime = "image/gif"
            elif data.startswith(b"RIFF"):
                mime = "image/webp"

            b64 = base64.b64encode(data).decode("utf-8")
            return f"data:{mime};base64,{b64}"
        except Exception as e:
            logger.warning(f"图片转base64失败 {image_path}: {e}")
            return None

    def _get_default_avatar(self) -> str:
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="50" fill="#8B0000"/><text x="50" y="58" font-size="40" fill="#fff" text-anchor="middle">?</text></svg>'
        b64 = base64.b64encode(svg.encode()).decode()
        return f"data:image/svg+xml;base64,{b64}"