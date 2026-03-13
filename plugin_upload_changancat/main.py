"""ChanganCat

长安猫插件，提供：
- 表情包统计（每日榜单，从morechatplus读取）
- 哈气统计（日榜、周榜，从morechatplus读取）
- 每日定时统计报告（支持配置指定群号，带开关）
- 复读功能（修复表情包复读，从morechatplus读取历史消息）
- 个人哈气详情查询（支持图片渲染）
- 哈气周榜（按天分组显示最近7天统计）
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

        # 群名缓存 {内部ID: 群名}
        self._group_name_cache: dict = {}
        # 真实origin缓存 {内部ID: 真实origin}，用于日报发送
        self._real_origin_cache: dict = {}

        # 字体缓存
        self._font_cache = {}

        # 任务引用和状态标志
        self._daily_report_task_ref: Optional[asyncio.Task] = None
        self._cleanup_task_ref: Optional[asyncio.Task] = None
        self._tasks_started: bool = False

        logger.info("[ChanganCat] 插件初始化完成，等待第一条消息启动后台任务...")

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

        font_paths = [
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]

        font = None
        for font_path in font_paths:
            try:
                if os.path.exists(font_path):
                    font = ImageFont.truetype(font_path, size)
                    break
            except:
                continue

        if font is None:
            font = ImageFont.load_default()
            logger.warning("[ChanganCat] 未找到中文字体，使用默认字体")

        self._font_cache[size] = font
        return font

    def _render_haqi_detail_image(self, data: dict, save_path: str) -> bool:
        """渲染哈气详情为图片"""
        if not PIL_AVAILABLE:
            return False

        try:
            width = 600
            margin = 40
            line_height = 25
            header_height = 80
            footer_height = 60
            max_chars_per_line = 28

            text_messages = data.get("text_messages", [])
            meme_haqi = data.get("meme_haqi", {})

            content_height = 0
            content_height += header_height

            if text_messages:
                content_height += 40
                for msg in text_messages:
                    display_text = f"[{msg.get('time', '01-01 00:00:00')}] {msg.get('content', '')}"
                    lines = max(1, len(display_text) // max_chars_per_line + (1 if len(display_text) % max_chars_per_line > 0 else 0))
                    content_height += lines * line_height + 10
            else:
                content_height += 40

            content_height += 30

            if meme_haqi:
                content_height += 40
                content_height += len(meme_haqi) * line_height
            else:
                content_height += 40

            content_height += footer_height
            total_height = content_height + margin * 2

            img = PILImage.new('RGB', (width, total_height), color=(255, 255, 255))
            draw = ImageDraw.Draw(img)

            title_font = self._get_font(24)
            header_font = self._get_font(18)
            content_font = self._get_font(16)
            small_font = self._get_font(14)

            y = margin

            for i in range(header_height):
                color_val = int(100 + (200 - 100) * (i / header_height))
                draw.line([(0, y + i), (width, y + i)], fill=(color_val, color_val, 255))

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

            draw.text((margin, y), "📱 文字哈气记录", fill=(50, 50, 50), font=header_font)
            y += 35

            if text_messages:
                for i, msg_data in enumerate(text_messages, 1):
                    time_str = msg_data.get("time", "01-01 00:00:00")
                    content = msg_data.get("content", "")
                    display_text = f"[{time_str}] {content}"

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

            y += 10
            draw.line([(margin, y), (width - margin, y)], fill=(200, 200, 200), width=1)
            y += 20

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

            y = total_height - footer_height
            draw.line([(margin, y), (width - margin, y)], fill=(230, 230, 230), width=1)
            footer_text = f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | ChanganCat"
            draw.text((width // 2, y + 20), footer_text, fill=(150, 150, 150), font=small_font, anchor="mm")

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

    def _start_background_tasks(self):
        """启动后台定时任务（懒加载）"""
        if self._tasks_started:
            return

        self._tasks_started = True
        logger.info("[ChanganCat] 正在启动后台定时任务...")

        if self.config.core.enable and self.config.core.enable_daily_report:
            try:
                self._daily_report_task_ref = asyncio.create_task(
                    self._daily_report_task(), 
                    name="ChanganCat_DailyReport"
                )
                logger.info("[ChanganCat] 每日报告任务已启动")
            except Exception as e:
                logger.error(f"[ChanganCat] 启动每日报告任务失败: {e}")

        try:
            self._cleanup_task_ref = asyncio.create_task(
                self._cleanup_task(), 
                name="ChanganCat_Cleanup"
            )
            logger.info("[ChanganCat] 清理任务已启动")
        except Exception as e:
            logger.error(f"[ChanganCat] 启动清理任务失败: {e}")

    async def _daily_report_task(self):
        """每日报告定时任务"""
        logger.info("[ChanganCat] 每日报告任务进入主循环")

        await asyncio.sleep(30)

        while True:
            try:
                now = datetime.now()
                target_hour = self.config.core.daily_report_hour
                target_minute = self.config.core.daily_report_minute

                target_time = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                if now >= target_time:
                    target_time = target_time + timedelta(days=1)

                wait_seconds = (target_time - now).total_seconds()
                logger.info(f"[ChanganCat] 下次报告时间: {target_time.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_seconds:.0f} 秒)")

                await asyncio.sleep(wait_seconds)

                if not self.config.core.enable or not self.config.core.enable_daily_report:
                    logger.info("[ChanganCat] 配置已禁用，跳过本次发送")
                    await asyncio.sleep(60)
                    continue

                logger.info("[ChanganCat] 开始发送每日哈气榜...")
                await self._send_daily_reports()
                logger.info("[ChanganCat] 每日哈气榜发送完成")

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                logger.info("[ChanganCat] 每日报告任务被取消")
                break
            except Exception as e:
                logger.error(f"[ChanganCat] 每日报告任务出错: {e}", exc_info=True)
                await asyncio.sleep(300)

    async def _cleanup_task(self):
        """定时清理任务"""
        while True:
            try:
                await asyncio.sleep(86400)
                if not self.config.core.enable:
                    break
                self.stats_manager.cleanup_old_stats()
                self._cleanup_temp_images()
            except asyncio.CancelledError:
                break
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

    def _extract_internal_id(self, origin: str) -> str:
        """从真实origin提取内部ID (qq_group_XXX)，用于缓存"""
        match = re.search(r'(\d+)$', origin)
        if match:
            group_id = match.group(1)
            return f"qq_group_{group_id}"
        return origin

    def _get_real_origin(self, internal_id: str) -> Optional[str]:
        """从内部ID获取缓存的真实origin（用于发送消息）"""
        return self._real_origin_cache.get(internal_id)

    async def _send_text_to_origin(self, internal_id: str, text: str):
        """发送纯文本消息（使用缓存的真实origin）"""
        try:
            from astrbot.api.message_components import Plain

            real_origin = self._get_real_origin(internal_id)
            if not real_origin:
                logger.error(f"[ChanganCat] 未找到群 {internal_id} 的真实origin，无法发送消息")
                raise ValueError(f"未缓存群 {internal_id} 的origin，请先在群内发送消息")

            msg = SimpleMessage([Plain(text)])
            await self.context.send_message(real_origin, msg)
            logger.info(f"[ChanganCat] 已发送消息到 {internal_id} ({real_origin})")

        except Exception as e:
            logger.error(f"[ChanganCat] 发送消息到 {internal_id} 失败: {e}")
            raise

    async def _send_daily_reports(self):
        """发送每日报告到所有目标群"""
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

            for internal_id in origins:
                try:
                    if internal_id not in self._real_origin_cache:
                        logger.warning(f"[ChanganCat] 群 {internal_id} 未激活（无真实origin缓存），跳过发送")
                        continue

                    await self._send_daily_report_to_group(internal_id)
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"[ChanganCat] 发送每日报告到 {internal_id} 失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"[ChanganCat] 发送每日报告失败: {e}", exc_info=True)

    async def _send_daily_report_to_group(self, internal_id: str):
        """发送每日报告到指定群（使用真实origin查询和发送）"""
        try:
            # 获取真实origin（用于查询数据库和发送消息）
            real_origin = self._get_real_origin(internal_id)
            if not real_origin:
                logger.error(f"[ChanganCat] 无法发送日报到 {internal_id}：未缓存真实origin")
                return

            group_name = self._get_group_name(internal_id)

            # 使用真实origin查询数据（与/哈气榜一致）
            report_text = self.stats_manager.format_haqi_command_response(real_origin, group_name)

            # 使用真实origin发送消息
            await self._send_text_to_origin(internal_id, report_text)

            logger.info(f"[ChanganCat] 已发送每日哈气榜到 {internal_id}")
        except Exception as e:
            logger.error(f"[ChanganCat] 发送每日报告到 {internal_id} 失败: {e}", exc_info=True)
            raise

    def _get_all_origins_from_morechatplus(self) -> List[str]:
        """从morechatplus获取所有有记录的群（返回内部ID格式）"""
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
                # 转换为内部ID格式
                origins = []
                for row in rows:
                    origin = row["origin"]
                    if origin.startswith("qq_group_"):
                        origins.append(origin)
                    else:
                        origins.append(self._extract_internal_id(origin))
                return origins
        except Exception as e:
            logger.debug(f"[ChanganCat] 获取群列表失败: {e}")
            return []

    def _get_group_name(self, internal_id: str) -> str:
        """获取群名"""
        if internal_id in self._group_name_cache:
            return self._group_name_cache[internal_id]

        match = re.search(r'qq_group_(\d+)', internal_id)
        if match:
            group_id = match.group(1)
            return f"QQ群 {group_id}"

        return internal_id

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理消息（启动定时任务 + 复读检测 + 缓存origin和群名）"""
        if not self._tasks_started and self.config.core.enable:
            self._start_background_tasks()

        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        msg_info = await self._extract_message_info(event)
        if not msg_info:
            return

        message_id, user_id, nickname, content, image_urls = msg_info
        real_origin = event.unified_msg_origin
        internal_id = self._extract_internal_id(real_origin)

        # 缓存真实origin（用于日报发送）
        if internal_id not in self._real_origin_cache:
            self._real_origin_cache[internal_id] = real_origin
            logger.info(f"[ChanganCat] 已缓存群 {internal_id} 的真实origin: {real_origin}")

        # 缓存群名（修正：尝试获取真实群名）
        if internal_id not in self._group_name_cache:
            group_name = None
            try:
                if hasattr(event.message_obj, 'group_name') and event.message_obj.group_name:
                    group_name = event.message_obj.group_name
                elif hasattr(event, 'group_name') and event.group_name:
                    group_name = event.group_name
            except Exception:
                pass

            if group_name:
                self._group_name_cache[internal_id] = group_name
                logger.info(f"[ChanganCat] 已缓存群 {internal_id} 的名称为: {group_name}")

        logger.debug(f"[ChanganCat] 收到消息: {nickname}({user_id}): {content[:50]}...")

        if self.config.repeat.enable:
            repeat_info = self.repeat_manager.check_and_record_message(
                internal_id, message_id, user_id, content, image_urls
            )

            if not repeat_info:
                repeat_info = self.repeat_manager.check_repeat_from_morechatplus(internal_id)

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

            from astrbot.api.message_components import Plain, Image as CompImage

            chain = []

            if is_meme:
                memes = self.stats_manager.extract_memes(content)

                if memes:
                    for idx, img_id in memes:
                        local_path = self.stats_manager._get_image_local_path(img_id)

                        if local_path and Path(local_path).exists():
                            try:
                                chain.append(CompImage(file=local_path))
                            except Exception as e:
                                logger.error(f"[ChanganCat] 添加表情包失败: {e}")
                        else:
                            if idx <= len(image_urls) and image_urls[idx - 1]:
                                url = image_urls[idx - 1]
                                if url.startswith("http"):
                                    local_path = await self._download_image(url)
                                    if local_path:
                                        chain.append(CompImage(file=local_path))
                                elif Path(url).exists():
                                    chain.append(CompImage(file=url))
                else:
                    for url in image_urls[:3]:
                        if not url or not isinstance(url, str):
                            continue
                        try:
                            is_local = url.startswith("/") or (len(url) > 1 and url[1] == ":")
                            if is_local:
                                if Path(url).exists():
                                    chain.append(CompImage(file=url))
                            else:
                                local_path = await self._download_image(url)
                                if local_path:
                                    chain.append(CompImage(file=local_path))
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

    @filter.command("哈气榜")
    async def cmd_haqi_ranking(self, event: AstrMessageEvent):
        """哈气榜命令 - 使用真实origin查询（与数据库存储格式一致）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        # 使用真实origin查询数据库（恢复原有行为）
        origin = event.unified_msg_origin
        # 尝试获取群名（如果已缓存）
        internal_id = self._extract_internal_id(origin)
        group_name = self._get_group_name(internal_id)

        try:
            # 使用真实origin查询数据（因为数据库存储的是aiocqhttp:GroupMessage:XXX格式）
            response = self.stats_manager.format_haqi_command_response(origin, group_name)
            await self._safe_send(event, response)
            logger.info(f"[ChanganCat] 已响应/哈气榜命令")
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 哈气榜命令出错: {e}")
            await self._safe_send(event, f"获取哈气榜失败: {e}")
            event.stop_event()

    @filter.command("哈气周榜")
    async def cmd_daily_haqi_ranking(self, event: AstrMessageEvent):
        """哈气周榜命令 - 按天显示最近7天的哈气统计"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        internal_id = self._extract_internal_id(origin)
        group_name = self._get_group_name(internal_id)

        try:
            response = self.stats_manager.format_daily_haqi_report(origin, group_name, days=7)
            await self._safe_send(event, response)
            logger.info(f"[ChanganCat] 已响应/哈气周榜命令")
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 哈气周榜命令出错: {e}")
            await self._safe_send(event, f"获取哈气周榜失败: {e}")
            event.stop_event()

    @filter.command("表情包榜")
    async def cmd_meme_ranking(self, event: AstrMessageEvent):
        """表情包榜命令 - 使用真实origin查询"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        internal_id = self._extract_internal_id(origin)
        group_name = self._get_group_name(internal_id)

        try:
            response, meme_images = self.stats_manager.format_meme_command_response(origin, group_name)
            await self._send_message_with_images(event.unified_msg_origin, response, meme_images)
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

    @filter.command("test_daily_report")
    async def cmd_test_daily_report(self, event: AstrMessageEvent):
        """测试每日日报（立即触发一次）"""
        if not self.config.core.enable:
            return

        origin = event.unified_msg_origin
        internal_id = self._extract_internal_id(origin)

        # 确保缓存当前群
        if internal_id not in self._real_origin_cache:
            self._real_origin_cache[internal_id] = origin
            logger.info(f"[ChanganCat] 测试前已缓存当前群: {internal_id}")

        await self._safe_send(event, "正在测试发送每日哈气榜...")
        logger.info("[ChanganCat] 手动触发每日报告测试")

        try:
            # 只发送当前群
            await self._send_daily_report_to_group(internal_id)
            await self._safe_send(event, "测试发送完成")
        except Exception as e:
            logger.error(f"[ChanganCat] 测试发送失败: {e}", exc_info=True)
            await self._safe_send(event, f"测试发送失败: {e}")
        finally:
            event.stop_event()

    def _extract_at_target(self, event: AstrMessageEvent) -> Optional[str]:
        """提取@的目标用户ID"""
        for comp in event.get_messages():
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    @filter.command("今日哈气")
    async def cmd_today_haqi_detail(self, event: AstrMessageEvent):
        """今日哈气详情"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        target_id = self._extract_at_target(event)
        if not target_id:
            await self._safe_send(event, "请@想要查询的群友，例如：/今日哈气 @张三")
            event.stop_event()
            return

        origin = event.unified_msg_origin

        try:
            data = self.stats_manager.get_user_haqi_details(origin, target_id, days=1)

            if data["total_count"] == 0:
                nickname = data.get("nickname", f"用户{target_id}")
                await self._safe_send(event, f"{nickname} 今日没有哈气记录~")
                event.stop_event()
                return

            if PIL_AVAILABLE:
                img_path = self.temp_dir / f"haqi_today_{target_id}_{int(datetime.now().timestamp())}.png"
                success = self._render_haqi_detail_image(data, str(img_path))

                if success and img_path.exists():
                    from astrbot.api.message_components import Image as CompImage
                    msg = SimpleMessage([CompImage(file=str(img_path))])
                    await self.context.send_message(event.unified_msg_origin, msg)
                    logger.info(f"[ChanganCat] 已发送今日哈气图片报告 for {target_id}")
                else:
                    text = self._format_haqi_detail_text(data)
                    await self._safe_send(event, text)
            else:
                text = self._format_haqi_detail_text(data)
                await self._safe_send(event, text)

            event.stop_event()

        except Exception as e:
            logger.error(f"[ChanganCat] 今日哈气命令出错: {e}")
            await self._safe_send(event, f"获取今日哈气详情失败: {e}")
            event.stop_event()

    @filter.command("七日哈气")
    async def cmd_week_haqi_detail(self, event: AstrMessageEvent):
        """七日哈气详情"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        target_id = self._extract_at_target(event)
        if not target_id:
            await self._safe_send(event, "请@想要查询的群友，例如：/七日哈气 @张三")
            event.stop_event()
            return

        origin = event.unified_msg_origin

        try:
            data = self.stats_manager.get_user_haqi_details(origin, target_id, days=7)

            if data["total_count"] == 0:
                nickname = data.get("nickname", f"用户{target_id}")
                await self._safe_send(event, f"{nickname} 近七日没有哈气记录~")
                event.stop_event()
                return

            if PIL_AVAILABLE:
                img_path = self.temp_dir / f"haqi_week_{target_id}_{int(datetime.now().timestamp())}.png"
                success = self._render_haqi_detail_image(data, str(img_path))

                if success and img_path.exists():
                    from astrbot.api.message_components import Image as CompImage
                    msg = SimpleMessage([CompImage(file=str(img_path))])
                    await self.context.send_message(event.unified_msg_origin, msg)
                    logger.info(f"[ChanganCat] 已发送七日哈气图片报告 for {target_id}")
                else:
                    text = self._format_haqi_detail_text(data)
                    await self._safe_send(event, text)
            else:
                text = self._format_haqi_detail_text(data)
                await self._safe_send(event, text)

            event.stop_event()

        except Exception as e:
            logger.error(f"[ChanganCat] 七日哈气命令出错: {e}")
            await self._safe_send(event, f"获取七日哈气详情失败: {e}")
            event.stop_event()

    def _format_haqi_detail_text(self, data: dict) -> str:
        """格式化哈气详情为文字"""
        lines = []
        nickname = data.get("nickname", "用户")
        days = data.get("days", 1)
        period = "今日" if days == 1 else "七日"

        lines.append(f"📊 {nickname} 的{period}哈气详情")
        lines.append(f"总计: {data['total_count']}次 (文字{data['text_count']} + 表情包{data['meme_count']})")
        lines.append("")

        lines.append("📱 文字哈气记录:")
        if data["text_messages"]:
            for i, msg_data in enumerate(data["text_messages"][:20], 1):
                time_str = msg_data.get("time", "01-01 00:00:00")
                content = msg_data.get("content", "")
                lines.append(f"{i}. [{time_str}] {content}")
            if len(data["text_messages"]) > 20:
                lines.append(f"... 还有 {len(data['text_messages']) - 20} 条")
        else:
            lines.append("无")

        lines.append("")

        lines.append("🖼️ 表情包哈气统计:")
        if data["meme_haqi"]:
            for img_id, count in sorted(data["meme_haqi"].items(), key=lambda x: -x[1]):
                lines.append(f"  {img_id}: {count}次")
        else:
            lines.append("无")

        return chr(10).join(lines)

    async def _safe_send(self, event: AstrMessageEvent, text: str):
        """安全发送文本消息"""
        origin = event.unified_msg_origin
        try:
            from astrbot.api.message_components import Plain
            msg = SimpleMessage([Plain(text)])
            await self.context.send_message(origin, msg)
        except Exception as e:
            logger.error(f"[ChanganCat] 发送失败: {e}")

    async def _send_message_with_images(self, origin: str, text: str, images: List[dict] = None):
        """发送消息（带图片）"""
        try:
            from astrbot.api.message_components import Plain, Image as CompImage

            chain = []
            lines = text.split(chr(10))
            image_idx = 0

            for line in lines:
                if line.strip():
                    chain.append(Plain(line + chr(10)))
                else:
                    chain.append(Plain(chr(10)))

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
                                chain.append(CompImage(file=img_path))
                                chain.append(Plain(chr(10)))
                                image_idx += 1
                            except Exception as e:
                                logger.error(f"[ChanganCat] 创建图片失败: {e}")
                                image_idx += 1
                        else:
                            image_idx += 1

            if chain:
                await self.context.send_message(origin, SimpleMessage(chain))

        except Exception as e:
            logger.error(f"[ChanganCat] 发送消息失败: {e}")

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

        lines.append(f"每日播报: {'开启' if self.config.core.enable_daily_report else '关闭'}")

        if self._daily_report_task_ref and not self._daily_report_task_ref.done():
            lines.append("定时任务状态: 运行中")
        else:
            lines.append("定时任务状态: 未运行")

        if self._real_origin_cache:
            lines.append(f"已缓存群号: {', '.join(self._real_origin_cache.keys())}")
        else:
            lines.append("已缓存群号: 无")

        lines.append(f"图片渲染: {'可用' if PIL_AVAILABLE else '不可用'}")

        return chr(10).join(lines)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[ChanganCat] 插件终止，清理后台任务...")
        if self._daily_report_task_ref and not self._daily_report_task_ref.done():
            self._daily_report_task_ref.cancel()
            try:
                await self._daily_report_task_ref
            except asyncio.CancelledError:
                pass
        if self._cleanup_task_ref and not self._cleanup_task_ref.done():
            self._cleanup_task_ref.cancel()
            try:
                await self._cleanup_task_ref
            except asyncio.CancelledError:
                pass