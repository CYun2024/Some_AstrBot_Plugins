"""ChanganCat

长安猫插件，提供：
- 表情包统计（每日榜单，从morechatplus读取）
- 哈气统计（日榜、周榜，从morechatplus读取）
- 每日定时统计报告（支持配置指定群号）
- 复读功能（从morechatplus读取历史消息）
- 个人哈气详情查询（支持图片渲染）
"""

import asyncio
import hashlib
import re
import aiohttp
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .database import DatabaseManager
from .plugin_config import PluginConfig, parse_plugin_config
from .repeat_manager import RepeatManager
from .stats_manager import StatsManager

# 图片生成相关导入
try:
    from PIL import Image as PILImage, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("[ChanganCat] PIL库未安装，图片渲染功能将不可用")


class SimpleMessage:
    """简单的消息包装类，用于兼容 context.send_message"""
    def __init__(self, components):
        self.chain = components if isinstance(components, list) else [components]


class ChanganCatPlugin(star.Star):
    """长安猫插件"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = parse_plugin_config(config)

        # 初始化数据库（仅用于复读记录和配置）
        plugin_data_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "changancat"
        )
        self.db = DatabaseManager(plugin_data_dir / "changancat.db")

        # 临时下载目录（用于网络图片）
        self.temp_dir = plugin_data_dir / "temp_images"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 初始化管理器
        self.stats_manager = StatsManager(self.db, self.config)
        self.repeat_manager = RepeatManager(self.db, self.config)

        # 连接morechatplus数据库
        self._init_morechatplus_connection()

        # 群名缓存
        self._group_name_cache: dict = {}

        # 字体缓存
        self._font_cache = {}

        logger.info("[ChanganCat] 插件初始化完成")

    def _init_morechatplus_connection(self):
        """初始化与morechatplus的连接"""
        try:
            morechatplus_db_path = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "morechatplus"
                / "chat_data.db"
            )
            if morechatplus_db_path.exists():
                self.stats_manager.set_morechatplus_db_path(str(morechatplus_db_path))
                self.repeat_manager.set_morechatplus_db_path(str(morechatplus_db_path))
                logger.info(f"[ChanganCat] 已连接到morechatplus数据库")
            else:
                logger.warning(f"[ChanganCat] morechatplus数据库不存在，功能将受限")
        except Exception as e:
            logger.warning(f"[ChanganCat] 连接morechatplus数据库失败: {e}")

    def _get_font(self, size: int = 20):
        """获取中文字体（支持多平台）"""
        if size in self._font_cache:
            return self._font_cache[size]

        # 常见中文字体路径（按优先级）
        font_paths = [
            # Linux
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            # Windows
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            # macOS
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]

        font = None
        for font_path in font_paths:
            try:
                if os.path.exists(font_path):
                    font = ImageFont.truetype(font_path, size)
                    logger.debug(f"[ChanganCat] 使用字体: {font_path}")
                    break
            except:
                continue

        if font is None:
            # 使用默认字体（可能不支持中文）
            font = ImageFont.load_default()
            logger.warning("[ChanganCat] 未找到中文字体，使用默认字体")

        self._font_cache[size] = font
        return font

    def _render_haqi_detail_image(self, data: dict, save_path: str) -> bool:
        """渲染哈气详情为图片

        尺寸调整指南:
        - width: 图片宽度，默认800px（QQ推荐宽度，太宽可能显示不全）
        - margin: 左右边距，默认40px（文字距离边缘的距离）
        - line_height: 行高，默认30px（每行文字占的高度）
        - header_height: 标题栏高度，默认80px
        - footer_height: 页脚高度，默认60px

        Args:
            data: get_user_haqi_details 返回的数据
            save_path: 图片保存路径

        Returns:
            是否成功
        """
        if not PIL_AVAILABLE:
            return False

        try:
            # ==================== 图片参数调整区 ====================
            width = 600           # 图片宽度（像素），QQ建议800-1000
            margin = 40           # 左右边距（像素）
            line_height = 25      # 文字行高（像素）
            header_height = 80    # 顶部标题栏高度
            footer_height = 60    # 底部页脚高度
            max_chars_per_line = 28  # 每行最大字符数（根据字体大小调整）
            # =====================================================

            # 计算内容高度
            text_messages = data.get("text_messages", [])
            meme_haqi = data.get("meme_haqi", {})

            content_height = 0

            # 标题区域
            content_height += header_height

            # 文字哈气区域
            if text_messages:
                content_height += 40  # 小标题
                for msg in text_messages:
                    # 估算文本高度（粗略计算），现在包含时间前缀 [HH:MM:SS] 
                    display_text = f"[{msg.get('time', '01-01 00:00:00')}] {msg.get('content', '')}"
                    lines = max(1, len(display_text) // max_chars_per_line + (1 if len(display_text) % max_chars_per_line > 0 else 0))
                    content_height += lines * line_height + 10
            else:
                content_height += 40

            # 分隔线
            content_height += 30

            # 表情包哈气区域
            if meme_haqi:
                content_height += 40  # 小标题
                content_height += len(meme_haqi) * line_height
            else:
                content_height += 40

            # 页脚
            content_height += footer_height

            total_height = content_height + margin * 2

            # 创建图片
            img = PILImage.new('RGB', (width, total_height), color=(255, 255, 255))
            draw = ImageDraw.Draw(img)

            # 获取字体
            title_font = self._get_font(24)
            header_font = self._get_font(18)
            content_font = self._get_font(16)
            small_font = self._get_font(14)

            y = margin

            # 绘制标题背景（渐变蓝色）
            for i in range(header_height):
                color_val = int(100 + (200 - 100) * (i / header_height))
                draw.line([(0, y + i), (width, y + i)], fill=(color_val, color_val, 255))

            # 标题文字
            nickname = data.get("nickname", "用户")
            days = data.get("days", 1)
            title = f"{nickname} 的{'今日' if days == 1 else '七日'}哈气报告"
            draw.text((width // 2, y + 25), title, fill=(255, 255, 255), font=title_font, anchor="mm")

            total = data.get("total_count", 0)
            text_c = data.get("text_count", 0)
            meme_c = data.get("meme_count", 0)
            subtitle = f"总计: {total}次 (文字{text_c} + 表情包{meme_c})"
            draw.text((width // 2, y + 55), subtitle, fill=(240, 240, 240), font=small_font, anchor="mm")

            y += header_height + 20

            # 文字哈气详情
            draw.text((margin, y), "📱 文字哈气记录", fill=(50, 50, 50), font=header_font)
            y += 35

            if text_messages:
                for i, msg_data in enumerate(text_messages, 1):
                    time_str = msg_data.get("time", "01-01 00:00:00")
                    content = msg_data.get("content", "")
                    # 组合显示：[时间] 内容
                    display_text = f"[{time_str}] {content}"

                    # 文本换行处理
                    lines = []
                    current_line = ""
                    for char in display_text:
                        test_line = current_line + char
                        bbox = draw.textbbox((0, 0), test_line, font=content_font)
                        if bbox[2] - bbox[0] > width - margin * 2:
                            lines.append(current_line)
                            current_line = char
                        else:
                            current_line = test_line
                    lines.append(current_line)

                    for line in lines:
                        draw.text((margin, y), line, fill=(80, 80, 80), font=content_font)
                        y += line_height
                    y += 5
            else:
                draw.text((margin, y), "暂无文字哈气记录", fill=(150, 150, 150), font=content_font)
                y += 30

            # 分隔线
            y += 10
            draw.line([(margin, y), (width - margin, y)], fill=(200, 200, 200), width=1)
            y += 20

            # 表情包哈气统计
            draw.text((margin, y), "🖼️ 表情包哈气统计", fill=(50, 50, 50), font=header_font)
            y += 35

            if meme_haqi:
                for img_id, count in sorted(meme_haqi.items(), key=lambda x: -x[1]):
                    line = f"• {img_id}: {count}次"
                    draw.text((margin, y), line, fill=(80, 80, 80), font=content_font)
                    y += line_height
            else:
                draw.text((margin, y), "暂无表情包哈气记录", fill=(150, 150, 150), font=content_font)
                y += 30

            # 页脚
            y = total_height - footer_height
            draw.line([(margin, y), (width - margin, y)], fill=(230, 230, 230), width=1)
            footer_text = f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | ChanganCat"
            draw.text((width // 2, y + 20), footer_text, fill=(150, 150, 150), font=small_font, anchor="mm")

            # 保存图片
            img.save(save_path, quality=95)
            logger.info(f"[ChanganCat] 哈气详情图片已保存: {save_path}")
            return True

        except Exception as e:
            logger.error(f"[ChanganCat] 渲染图片失败: {e}")
            return False

    async def _download_image(self, url: str) -> Optional[str]:
        """下载网络图片到本地临时目录"""
        if not url or not url.startswith("http"):
            return None

        url_hash = hashlib.md5(url.encode()).hexdigest()
        ext = ".gif" if ".gif" in url.lower() else ".jpg"
        temp_path = self.temp_dir / f"{url_hash}{ext}"

        if temp_path.exists():
            return str(temp_path)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        content = await response.read()
                        with open(temp_path, "wb") as f:
                            f.write(content)
                        logger.debug(f"[ChanganCat] 下载图片成功: {url} -> {temp_path}")
                        return str(temp_path)
                    else:
                        logger.warning(f"[ChanganCat] 下载图片失败，状态码: {response.status}")
        except Exception as e:
            logger.error(f"[ChanganCat] 下载图片异常: {e}")

        return None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
        if self.config.core.enable:
            asyncio.create_task(self._daily_report_task())
            asyncio.create_task(self._cleanup_task())

            target_groups = self.config.core.target_groups
            groups_info = f"群号: {', '.join(target_groups)}" if target_groups else "群号: 未配置"

            logger.info(
                f"[ChanganCat] 插件已启用 | {groups_info} | "
                f"复读={'开启' if self.config.repeat.enable else '关闭'} | "
                f"统计={'开启' if self.config.stats.enable_haqi_stats or self.config.stats.enable_meme_stats else '关闭'}"
            )
        else:
            logger.info("[ChanganCat] 插件已禁用")

    async def _daily_report_task(self):
        """每日报告定时任务"""
        while True:
            try:
                now = datetime.now()
                target_hour = self.config.core.daily_report_hour
                target_minute = self.config.core.daily_report_minute

                if now.hour < target_hour or (now.hour == target_hour and now.minute < target_minute):
                    next_report = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                else:
                    next_report = (now + timedelta(days=1)).replace(
                        hour=target_hour, minute=target_minute, second=0, microsecond=0
                    )

                wait_seconds = (next_report - now).total_seconds()
                logger.info(f"[ChanganCat] 下次每日报告: {next_report}, 等待 {wait_seconds:.0f} 秒")

                await asyncio.sleep(wait_seconds)
                await self._send_daily_reports()

            except Exception as e:
                logger.error(f"[ChanganCat] 每日报告任务出错: {e}")
                await asyncio.sleep(3600)

    async def _cleanup_task(self):
        """定时清理任务"""
        while True:
            try:
                await asyncio.sleep(86400)
                self.stats_manager.cleanup_old_stats()
                self._cleanup_temp_images()
            except Exception as e:
                logger.error(f"[ChanganCat] 清理任务出错: {e}")
                await asyncio.sleep(3600)

    def _cleanup_temp_images(self):
        """清理临时下载的图片"""
        try:
            cutoff = datetime.now() - timedelta(days=7)
            count = 0
            for f in self.temp_dir.iterdir():
                if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    f.unlink()
                    count += 1
            if count > 0:
                logger.info(f"[ChanganCat] 清理了 {count} 个临时图片文件")
        except Exception as e:
            logger.error(f"[ChanganCat] 清理临时图片失败: {e}")

    async def _send_daily_reports(self):
        """发送每日报告"""
        try:
            target_groups = self.config.core.target_groups

            if target_groups:
                origins = [f"qq_group_{group_id}" for group_id in target_groups]
                logger.info(f"[ChanganCat] 发送每日报告到配置的 {len(origins)} 个群")
            else:
                origins = self._get_all_origins_from_morechatplus()
                logger.info(f"[ChanganCat] 发送每日报告到自动获取的 {len(origins)} 个群")

            if not origins:
                logger.info("[ChanganCat] 没有找到需要发送报告的群")
                return

            for origin in origins:
                try:
                    await self._send_daily_report_to_group(origin)
                except Exception as e:
                    logger.error(f"[ChanganCat] 发送每日报告到 {origin} 失败: {e}")

        except Exception as e:
            logger.error(f"[ChanganCat] 发送每日报告失败: {e}")

    async def _send_daily_report_to_group(self, origin: str):
        """发送每日报告到指定群"""
        group_name = self._get_group_name(origin)
        report_text, meme_images = self.stats_manager.format_daily_report(origin, group_name)
        await self._send_message_to_origin_alternate(origin, report_text, meme_images)
        logger.info(f"[ChanganCat] 已发送每日报告到 {origin}")

    def _get_all_origins_from_morechatplus(self) -> List[str]:
        """从morechatplus获取所有有记录的群"""
        try:
            import sqlite3
            morechatplus_db_path = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "morechatplus"
                / "chat_data.db"
            )

            if not morechatplus_db_path.exists():
                return []

            with sqlite3.connect(morechatplus_db_path) as conn:
                conn.row_factory = sqlite3.Row
                yesterday = (datetime.now() - timedelta(days=1)).timestamp()
                rows = conn.execute(
                    "SELECT DISTINCT origin FROM messages WHERE timestamp >= ?",
                    (yesterday,)
                ).fetchall()
                return [row["origin"] for row in rows]
        except Exception as e:
            logger.debug(f"[ChanganCat] 获取群列表失败: {e}")
            return []

    def _get_group_name(self, origin: str) -> str:
        """获取群名"""
        if origin in self._group_name_cache:
            return self._group_name_cache[origin]

        match = re.search(r'qq_group_(\d+)', origin)
        if match:
            group_id = match.group(1)
            return f"QQ群 {group_id}"

        return origin

    async def _send_message_to_origin_alternate(self, origin: str, text: str, images: List[dict] = None):
        """发送消息到指定origin（交替模式）"""
        try:
            chain = []
            lines = text.split("\n")
            image_idx = 0

            for line in lines:
                if line.strip():
                    chain.append(Plain(line + "\n"))
                else:
                    chain.append(Plain("\n"))

                if re.match(r'^\d+\.\s*发送次数：\d+', line) and images and image_idx < len(images):
                    img_info = images[image_idx]
                    img_path = img_info.get("path")
                    is_local = img_info.get("is_local", False)

                    if img_path:
                        if not is_local and img_path.startswith("http"):
                            local_path = await self._download_image(img_path)
                            if local_path:
                                img_path = local_path
                                is_local = True
                            else:
                                image_idx += 1
                                continue

                        if Path(img_path).exists():
                            try:
                                chain.append(Image(file=img_path, sub_type=1))
                                chain.append(Plain("\n"))
                                image_idx += 1
                            except Exception:
                                try:
                                    chain.append(Image(file=img_path))
                                    chain.append(Plain("\n"))
                                    image_idx += 1
                                except Exception as e2:
                                    logger.error(f"[ChanganCat] 创建图片失败: {e2}")
                                    image_idx += 1
                        else:
                            image_idx += 1

            if chain:
                msg = SimpleMessage(chain)
                await self.context.send_message(origin, msg)

        except Exception as e:
            logger.error(f"[ChanganCat] 发送消息失败: {e}")

    async def _send_message_to_origin(self, origin: str, text: str, images: List[dict] = None):
        """发送消息到指定origin（备用方法）"""
        try:
            chain = []
            chain.append(Plain(text))

            if images:
                for img_info in images[:3]:
                    img_path = img_info.get("path")
                    is_local = img_info.get("is_local", False)

                    if not img_path:
                        continue

                    if not is_local and img_path.startswith("http"):
                        local_path = await self._download_image(img_path)
                        if local_path:
                            img_path = local_path
                        else:
                            continue

                    if Path(img_path).exists():
                        chain.append(Plain("\n"))
                        try:
                            chain.append(Image(file=img_path, sub_type=1))
                        except:
                            chain.append(Image(file=img_path))

            msg = SimpleMessage(chain)
            await self.context.send_message(origin, msg)

        except Exception as e:
            logger.error(f"[ChanganCat] 发送消息失败: {e}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理消息（仅用于复读检测）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        msg_info = await self._extract_message_info(event)
        if not msg_info:
            return

        message_id, user_id, nickname, content, image_urls = msg_info
        origin = event.unified_msg_origin

        logger.debug(f"[ChanganCat] 收到消息: {nickname}({user_id}): {content[:50]}...")

        if self.config.repeat.enable:
            repeat_info = self.repeat_manager.check_and_record_message(
                origin, message_id, user_id, content, image_urls
            )

            if not repeat_info:
                repeat_info = self.repeat_manager.check_repeat_from_morechatplus(origin)

            if repeat_info:
                await self._do_repeat(event, repeat_info)

    async def _extract_message_info(self, event: AstrMessageEvent) -> Optional[tuple]:
        """提取消息信息"""
        try:
            message_id = str(event.message_obj.message_id or "")
            user_id = str(event.get_sender_id() or "")
            nickname = event.message_obj.sender.nickname or "未知"

            content_parts = []
            image_urls = []

            for comp in event.get_messages():
                if isinstance(comp, Plain):
                    content_parts.append(comp.text)
                elif isinstance(comp, Image):
                    url = str(comp.url or comp.file or "").strip()
                    if url:
                        image_urls.append(url)
                        url_hash = hashlib.md5(url.encode()).hexdigest()
                        img_id = f"url_{url_hash[:16]}"
                        content_parts.append(f"[image:{len(image_urls)}:{img_id}]")
                elif isinstance(comp, At):
                    content_parts.append(f"[at:{comp.qq}]")

            content = " ".join(content_parts)
            return message_id, user_id, nickname, content, image_urls

        except Exception as e:
            logger.error(f"[ChanganCat] 提取消息信息失败: {e}")
            return None

    async def _do_repeat(self, event: AstrMessageEvent, repeat_info: dict):
        """执行复读"""
        try:
            content = repeat_info["content"]
            image_urls = repeat_info.get("image_urls", [])
            is_meme = repeat_info.get("is_meme", False)
            origin = event.unified_msg_origin

            chain = []

            if is_meme and image_urls:
                for url in image_urls[:3]:
                    if not url or not isinstance(url, str):
                        continue

                    try:
                        is_local = url.startswith("/") or (len(url) > 1 and url[1] == ":")

                        if is_local:
                            if Path(url).exists():
                                try:
                                    chain.append(Image(file=url, sub_type=1))
                                except:
                                    chain.append(Image(file=url))
                            else:
                                logger.warning(f"[ChanganCat] 复读图片不存在: {url}")
                        else:
                            local_path = await self._download_image(url)
                            if local_path:
                                try:
                                    chain.append(Image(file=local_path, sub_type=1))
                                except:
                                    chain.append(Image(file=local_path))
                            else:
                                logger.warning(f"[ChanganCat] 复读下载图片失败: {url}")
                    except Exception as img_e:
                        logger.error(f"[ChanganCat] 复读图片失败: {img_e}")
                        continue
            else:
                clean_content = re.sub(r'\[at:\d+\]', '', content)
                clean_content = re.sub(r'<引用:\d+>', '', clean_content)
                clean_content = re.sub(r'\[image:\d+:[^\]]+\]', '[图片]', clean_content)
                clean_content = clean_content.strip()

                if clean_content:
                    chain.append(Plain(clean_content))

            if chain:
                msg = SimpleMessage(chain)
                await self.context.send_message(origin, msg)
                logger.info(f"[ChanganCat] 复读消息: {content[:50]}...")

        except Exception as e:
            logger.error(f"[ChanganCat] 复读失败: {e}")

    # ==================== 原有命令 ====================

    @filter.command("哈气榜")
    async def cmd_haqi_ranking(self, event: AstrMessageEvent):
        """哈气榜命令"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        group_name = self._get_group_name(origin)

        try:
            response = self.stats_manager.format_haqi_command_response(origin, group_name)
            await self._safe_send(event, response)
            logger.info(f"[ChanganCat] 已响应/哈气榜命令")
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 哈气榜命令出错: {e}")
            await self._safe_send(event, f"获取哈气榜失败: {e}")
            event.stop_event()

    @filter.command("表情包榜")
    async def cmd_meme_ranking(self, event: AstrMessageEvent):
        """表情包榜命令"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        group_name = self._get_group_name(origin)

        try:
            response, meme_images = self.stats_manager.format_meme_command_response(origin, group_name)
            await self._send_message_to_origin_alternate(event.unified_msg_origin, response, meme_images)
            logger.info(f"[ChanganCat] 已响应/表情包榜命令")
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 表情包榜命令出错: {e}")
            await self._safe_send(event, f"获取表情包榜失败: {e}")
            event.stop_event()

    @filter.command("changancat_stats")
    async def cmd_stats(self, event: AstrMessageEvent):
        """统计信息命令"""
        if not self.config.core.enable:
            return

        try:
            stats_text = self._get_stats_text()
            await self._safe_send(event, stats_text)
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 获取统计信息失败: {e}")
            await self._safe_send(event, f"获取统计信息失败: {e}")
            event.stop_event()

    # ==================== 新增命令 ====================

    def _extract_at_target(self, event: AstrMessageEvent) -> Optional[str]:
        """提取@的目标用户ID"""
        for comp in event.get_messages():
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    @filter.command("今日哈气")
    async def cmd_today_haqi_detail(self, event: AstrMessageEvent):
        """今日哈气详情（图片渲染）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        # 提取@的目标
        target_id = self._extract_at_target(event)
        if not target_id:
            await self._safe_send(event, "请@想要查询的群友，例如：/今日哈气 @张三")
            event.stop_event()
            return

        origin = event.unified_msg_origin

        try:
            # 获取详情数据
            data = self.stats_manager.get_user_haqi_details(origin, target_id, days=1)

            if data["total_count"] == 0:
                nickname = data.get("nickname", f"用户{target_id}")
                await self._safe_send(event, f"{nickname} 今日没有哈气记录~")
                event.stop_event()
                return

            # 生成图片
            if PIL_AVAILABLE:
                img_path = self.temp_dir / f"haqi_today_{target_id}_{int(datetime.now().timestamp())}.png"
                success = self._render_haqi_detail_image(data, str(img_path))

                if success and img_path.exists():
                    # 发送图片
                    chain = [Image(file=str(img_path))]
                    msg = SimpleMessage(chain)
                    await self.context.send_message(event.unified_msg_origin, msg)
                    logger.info(f"[ChanganCat] 已发送今日哈气图片报告 for {target_id}")
                else:
                    # 图片生成失败，发送文字版
                    text = self._format_haqi_detail_text(data)
                    await self._safe_send(event, text)
            else:
                # PIL不可用，发送文字版
                text = self._format_haqi_detail_text(data)
                await self._safe_send(event, text)

            event.stop_event()

        except Exception as e:
            logger.error(f"[ChanganCat] 今日哈气命令出错: {e}")
            await self._safe_send(event, f"获取今日哈气详情失败: {e}")
            event.stop_event()

    @filter.command("七日哈气")
    async def cmd_week_haqi_detail(self, event: AstrMessageEvent):
        """七日哈气详情（图片渲染）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        # 提取@的目标
        target_id = self._extract_at_target(event)
        if not target_id:
            await self._safe_send(event, "请@想要查询的群友，例如：/七日哈气 @张三")
            event.stop_event()
            return

        origin = event.unified_msg_origin

        try:
            # 获取详情数据
            data = self.stats_manager.get_user_haqi_details(origin, target_id, days=7)

            if data["total_count"] == 0:
                nickname = data.get("nickname", f"用户{target_id}")
                await self._safe_send(event, f"{nickname} 近七日没有哈气记录~")
                event.stop_event()
                return

            # 生成图片
            if PIL_AVAILABLE:
                img_path = self.temp_dir / f"haqi_week_{target_id}_{int(datetime.now().timestamp())}.png"
                success = self._render_haqi_detail_image(data, str(img_path))

                if success and img_path.exists():
                    # 发送图片
                    chain = [Image(file=str(img_path))]
                    msg = SimpleMessage(chain)
                    await self.context.send_message(event.unified_msg_origin, msg)
                    logger.info(f"[ChanganCat] 已发送七日哈气图片报告 for {target_id}")
                else:
                    # 图片生成失败，发送文字版
                    text = self._format_haqi_detail_text(data)
                    await self._safe_send(event, text)
            else:
                # PIL不可用，发送文字版
                text = self._format_haqi_detail_text(data)
                await self._safe_send(event, text)

            event.stop_event()

        except Exception as e:
            logger.error(f"[ChanganCat] 七日哈气命令出错: {e}")
            await self._safe_send(event, f"获取七日哈气详情失败: {e}")
            event.stop_event()

    def _format_haqi_detail_text(self, data: dict) -> str:
        """格式化哈气详情为文字（备用）"""
        lines = []
        nickname = data.get("nickname", "用户")
        days = data.get("days", 1)
        period = "今日" if days == 1 else "七日"

        lines.append(f"📊 {nickname} 的{period}哈气详情")
        lines.append(f"总计: {data['total_count']}次 (文字{data['text_count']} + 表情包{data['meme_count']})")
        lines.append("")

        # 文字记录
        lines.append("📱 文字哈气记录:")
        if data["text_messages"]:
            for i, msg_data in enumerate(data["text_messages"][:20], 1):  # 最多显示20条
                time_str = msg_data.get("time", "01-01 00:00:00")
                content = msg_data.get("content", "")
                lines.append(f"{i}. [{time_str}] {content}")
            if len(data["text_messages"]) > 20:
                lines.append(f"... 还有 {len(data['text_messages']) - 20} 条")
        else:
            lines.append("无")

        lines.append("")

        # 表情包统计
        lines.append("🖼️ 表情包哈气统计:")
        if data["meme_haqi"]:
            for img_id, count in sorted(data["meme_haqi"].items(), key=lambda x: -x[1]):
                lines.append(f"  {img_id}: {count}次")
        else:
            lines.append("无")

        return "\n".join(lines)

    async def _safe_send(self, event: AstrMessageEvent, text: str):
        """安全发送文本消息"""
        origin = event.unified_msg_origin

        try:
            msg = SimpleMessage([Plain(text)])
            await self.context.send_message(origin, msg)
        except Exception as e:
            logger.error(f"[ChanganCat] 发送失败: {e}")

    def _get_stats_text(self) -> str:
        """获取插件统计信息"""
        lines = []
        lines.append("📊 ChanganCat 统计信息")
        lines.append("")

        try:
            import sqlite3
            with sqlite3.connect(self.db.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT COUNT(*) as cnt FROM repeat_records").fetchone()
                repeat_count = row["cnt"] if row else 0
                lines.append(f"复读记录: {repeat_count} 条")
        except Exception as e:
            lines.append(f"获取统计失败: {e}")

        morechatplus_db_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "morechatplus"
            / "chat_data.db"
        )
        if morechatplus_db_path.exists():
            lines.append("morechatplus连接: 正常")
            try:
                with sqlite3.connect(morechatplus_db_path) as conn:
                    today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM messages WHERE timestamp >= ?",
                        (today_start,)
                    ).fetchone()
                    lines.append(f"今日消息总数: {row['cnt'] if row else 0} 条")
            except:
                pass
        else:
            lines.append("morechatplus连接: 未找到数据库")

        target_groups = self.config.core.target_groups
        if target_groups:
            lines.append(f"每日报告目标群: {', '.join(target_groups)}")
        else:
            lines.append("每日报告目标群: 未配置（自动获取）")

        # 显示PIL状态
        lines.append(f"图片渲染: {'可用' if PIL_AVAILABLE else '不可用(缺少PIL库)'}")

        return "\n".join(lines)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[ChanganCat] 插件终止")