"""
晚报报告生成器
负责将帖子数据 + AI评论 组装成 HTML 晚报，并支持转图片
"""

import asyncio
import base64
import json
import os
import re
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

    def generate_evening_report(
        self,
        posts: list[dict],
        issue_no: int,
        report_date: str,
        community_name: str = "庭院社区",
        theme: str = "default",
    ) -> str:
        total_posts = len(posts)
        total_images = sum(
            len(p.get("image_paths", [])) if isinstance(p.get("image_paths"), list)
            else len(json.loads(p.get("image_paths", "[]")) if p.get("image_paths") else [])
            for p in posts
        )

        posts_html = self._render_posts_list(posts, theme)

        render_data = {
            "issue_no": issue_no,
            "report_date": report_date,
            "community_name": community_name,
            "total_posts": total_posts,
            "total_images": total_images,
            "posts_html": posts_html,
            "generation_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

        for img_path in image_paths[:3]:
            img_data = self._image_to_base64(img_path)
            if img_data:
                image_data_list.append(img_data)

        content = post.get("content", "") or ""
        content_cleaned = self._clean_content(content)
        if len(content_cleaned) > 300:
            content_cleaned = content_cleaned[:300] + "..."

        comment = post.get("comment", "") or "暂无评论"

        # daily_no 可能是字符串格式如 "20260620-1"
        daily_no = post.get("daily_no", 0)
        daily_no_str = str(daily_no) if daily_no else "0"

        return {
            "daily_no": daily_no_str,
            "title": post.get("title", "(无标题)"),
            "username": post.get("username", "未知用户"),
            "avatar_data": avatar_data,
            "create_at_str": post.get("create_at_str", ""),
            "content": content_cleaned,
            "comment": comment,
            "image_data_list": image_data_list,
            "image_count": len(image_data_list),
        }

    # ================================================================
    # 头像处理（修复版：支持下载网络头像）
    # ================================================================

    def _resolve_avatar(self, avatar: str) -> str:
        """解析头像，支持本地路径、HTTP URL，失败返回默认头像"""
        if not avatar:
            return self._get_default_avatar()

        # 1. 本地路径且存在
        if os.path.exists(avatar):
            return self._image_to_base64(avatar) or self._get_default_avatar()

        # 2. HTTP URL：尝试下载并缓存
        if avatar.startswith("http"):
            cached = self._get_cached_avatar(avatar)
            if cached:
                return cached
            # 同步下载（晚报生成通常在异步上下文外调用）
            try:
                downloaded = self._download_avatar_sync(avatar)
                if downloaded:
                    return downloaded
            except Exception as e:
                logger.warning(f"下载头像失败 {avatar}: {e}")
            return self._get_default_avatar()

        return self._get_default_avatar()

    def _get_cached_avatar(self, url: str) -> Optional[str]:
        """检查是否有缓存的头像 base64"""
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
        """缓存头像 base64"""
        import hashlib
        url_hash = hashlib.md5(url.encode()).hexdigest()
        cache_path = self.avatar_cache_dir / f"{url_hash}.txt"
        try:
            cache_path.write_text(base64_data, encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存头像缓存失败: {e}")

    def _download_avatar_sync(self, url: str) -> Optional[str]:
        """同步下载头像并转为 base64（用于非异步上下文）"""
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
        """异步下载头像并转为 base64"""
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
    # T2I 图片渲染（核心新增！）
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

    def save_report(self, html_content: str, filename: str = None) -> str:
        if not filename:
            filename = f"evening_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

        file_path = self.reports_dir / filename
        file_path.write_text(html_content, encoding="utf-8")
        logger.info(f"晚报 HTML 已保存: {file_path}")
        return str(file_path)

    def save_image(self, image_data: bytes, filename: str = None) -> str:
        """保存图片 bytes 到文件"""
        if not filename:
            filename = f"evening_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"

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
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="50" fill="#ddd"/></svg>'
        b64 = base64.b64encode(svg.encode()).decode()
        return f"data:image/svg+xml;base64,{b64}"

    def _clean_content(self, text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r'<[^>]+>', ' ', text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned