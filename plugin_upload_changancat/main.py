"""ChanganCat

长安猫插件，提供：
- 表情包统计（每日榜单，从morechatplus读取）
- 哈气统计（日榜、周榜，从morechatplus读取）
- 每日定时统计报告（支持配置指定群号）
- 复读功能（从morechatplus读取历史消息）
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

    async def _download_image(self, url: str) -> Optional[str]:
        """下载网络图片到本地临时目录

        Returns:
            本地文件路径，下载失败返回None
        """
        if not url or not url.startswith("http"):
            return None

        # 生成临时文件名（基于URL的MD5）
        url_hash = hashlib.md5(url.encode()).hexdigest()
        ext = ".gif" if ".gif" in url.lower() else ".jpg"
        temp_path = self.temp_dir / f"{url_hash}{ext}"

        # 如果已存在，直接返回
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
                        logger.warning(f"[ChanganCat] 下载图片失败，状态码: {response.status}, url: {url}")
        except Exception as e:
            logger.error(f"[ChanganCat] 下载图片异常: {e}, url: {url}")

        return None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
        if self.config.core.enable:
            # 启动定时任务
            asyncio.create_task(self._daily_report_task())
            # 启动清理任务
            asyncio.create_task(self._cleanup_task())

            # 显示配置的群号
            target_groups = self.config.core.target_groups
            groups_info = f"群号: {', '.join(target_groups)}" if target_groups else "群号: 未配置（将自动获取）"

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

                # 计算下次发送时间
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
                # 每天清理一次
                await asyncio.sleep(86400)
                self.stats_manager.cleanup_old_stats()
                # 清理临时图片（保留最近7天）
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
        """发送每日报告到配置的群（支持指定群号）"""
        try:
            target_groups = self.config.core.target_groups

            if target_groups:
                # 使用配置的群号列表
                origins = [f"qq_group_{group_id}" for group_id in target_groups]
                logger.info(f"[ChanganCat] 发送每日报告到配置的 {len(origins)} 个群: {origins}")
            else:
                # 回退到自动获取（从morechatplus数据库获取最近有消息的群）
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
        # 获取群名
        group_name = self._get_group_name(origin)

        # 生成报告（从morechatplus获取数据）
        report_text, meme_images = self.stats_manager.format_daily_report(origin, group_name)

        # 发送消息（修复：使用交替模式）
        await self._send_message_to_origin_alternate(origin, report_text, meme_images)

        logger.info(f"[ChanganCat] 已发送每日报告到 {origin}")

    def _get_all_origins_from_morechatplus(self) -> List[str]:
        """从morechatplus获取所有有记录的群（回退方案）"""
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
                # 获取最近24小时有消息的群
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

        # 尝试从origin解析
        # origin格式通常是 "qq_group_<group_id>"
        match = re.search(r'qq_group_(\d+)', origin)
        if match:
            group_id = match.group(1)
            return f"QQ群 {group_id}"

        return origin

    async def _send_message_to_origin_alternate(self, origin: str, text: str, images: List[dict] = None):
        """发送消息到指定origin（修复：文本和图片交替显示）

        格式：
        1. 发送次数：8 次
        [图片1]
        2. 发送次数：5 次
        [图片2]
        """
        try:
            chain = []

            # 分割文本为多行
            lines = text.split("\n")
            image_idx = 0

            for line in lines:
                # 添加当前行文本
                if line.strip():
                    chain.append(Plain(line + "\n"))
                else:
                    chain.append(Plain("\n"))

                # 检查这一行是否是排名行（如 "1. 发送次数：8 次"）
                if re.match(r'^\d+\.\s*发送次数：\d+', line) and images and image_idx < len(images):
                    img_info = images[image_idx]
                    img_path = img_info.get("path")
                    is_local = img_info.get("is_local", False)

                    if img_path:
                        # 如果是网络URL，先下载
                        if not is_local and img_path.startswith("http"):
                            local_path = await self._download_image(img_path)
                            if local_path:
                                img_path = local_path
                                is_local = True
                            else:
                                image_idx += 1
                                continue

                        # 检查文件是否存在
                        if Path(img_path).exists():
                            try:
                                # 修复：添加 sub_type=1 让图片以表情模式显示（较小尺寸）
                                # 注意：不同版本的AstrBot可能参数名不同，如果无效请尝试移除 sub_type 参数
                                chain.append(Image(file=img_path, sub_type=1))
                                chain.append(Plain("\n"))  # 图片后换行
                                image_idx += 1
                            except Exception as img_e:
                                # 如果 sub_type 参数不支持，尝试不使用
                                try:
                                    chain.append(Image(file=img_path))
                                    chain.append(Plain("\n"))
                                    image_idx += 1
                                except Exception as e2:
                                    logger.error(f"[ChanganCat] 创建图片失败: {e2}")
                                    image_idx += 1
                        else:
                            image_idx += 1

            # 使用 SimpleMessage 包装
            if chain:
                msg = SimpleMessage(chain)
                await self.context.send_message(origin, msg)

        except Exception as e:
            logger.error(f"[ChanganCat] 发送消息失败: {e}")
            # 回退到简单发送
            await self._send_message_to_origin(origin, text, images)

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
        """处理消息（仅用于复读检测，不存储消息）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        # 提取消息信息
        msg_info = await self._extract_message_info(event)
        if not msg_info:
            return

        message_id, user_id, nickname, content, image_urls = msg_info
        origin = event.unified_msg_origin

        logger.debug(f"[ChanganCat] 收到消息: {nickname}({user_id}): {content[:50]}...")

        # 检查复读（从morechatplus读取最近消息进行判断）
        if self.config.repeat.enable:
            # 先检查本地缓存（实时）
            repeat_info = self.repeat_manager.check_and_record_message(
                origin, message_id, user_id, content, image_urls
            )

            # 如果本地缓存没触发，再查数据库（防止重启后缓存丢失）
            if not repeat_info:
                repeat_info = self.repeat_manager.check_repeat_from_morechatplus(origin)

            if repeat_info:
                await self._do_repeat(event, repeat_info)

    async def _extract_message_info(self, event: AstrMessageEvent) -> Optional[tuple]:
        """提取消息信息（适配morechatplus的图片ID格式）"""
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
                        # 生成与morechatplus兼容的图片ID（基于URL的MD5）
                        # morechatplus使用 img_MD5前16位 格式
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
        """执行复读（修复：网络图片先下载，使用sub_type=1）"""
        try:
            content = repeat_info["content"]
            image_urls = repeat_info.get("image_urls", [])
            is_meme = repeat_info.get("is_meme", False)
            origin = event.unified_msg_origin

            # 构建消息链
            chain = []

            if is_meme and image_urls:
                # 如果是表情包，发送图片
                for url in image_urls[:3]:  # 最多3张
                    if not url or not isinstance(url, str):
                        continue

                    try:
                        # 检查是否是本地路径（以/或Windows盘符开头）
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
                            # 网络URL，先下载
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
                # 发送文本
                # 清理内容中的at标签和图片标记
                clean_content = re.sub(r'\[at:\d+\]', '', content)
                clean_content = re.sub(r'<引用:\d+>', '', clean_content)
                clean_content = re.sub(r'\[image:\d+:[^\]]+\]', '[图片]', clean_content)
                clean_content = clean_content.strip()

                if clean_content:
                    chain.append(Plain(clean_content))

            if chain:
                # 使用 SimpleMessage 包装
                msg = SimpleMessage(chain)
                await self.context.send_message(origin, msg)
                logger.info(f"[ChanganCat] 复读消息: {content[:50]}...")

        except Exception as e:
            logger.error(f"[ChanganCat] 复读失败: {e}")

    # ==================== 命令处理 ====================

    @filter.command("哈气榜")
    async def cmd_haqi_ranking(self, event: AstrMessageEvent):
        """哈气榜命令（从morechatplus读取数据）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        group_name = self._get_group_name(origin)

        try:
            # 生成哈气榜（从morechatplus读取）
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
        """表情包榜命令（从morechatplus读取今日数据，交替显示）"""
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        group_name = self._get_group_name(origin)

        try:
            # 生成表情包榜（从morechatplus读取今日数据）
            response, meme_images = self.stats_manager.format_meme_command_response(origin, group_name)

            logger.debug(f"[ChanganCat] 表情包榜: {len(meme_images)}张图片待发送")

            # 修复：使用交替发送模式
            await self._send_message_to_origin_alternate(event.unified_msg_origin, response, meme_images)

            logger.info(f"[ChanganCat] 已响应/表情包榜命令")
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 表情包榜命令出错: {e}")
            await self._safe_send(event, f"获取表情包榜失败: {e}")
            event.stop_event()

    @filter.command("changancat_stats")
    async def cmd_stats(self, event: AstrMessageEvent):
        """统计信息命令（管理员）"""
        if not self.config.core.enable:
            return

        try:
            # 获取统计信息
            stats_text = self._get_stats_text()
            await self._safe_send(event, stats_text)
            event.stop_event()
        except Exception as e:
            logger.error(f"[ChanganCat] 获取统计信息失败: {e}")
            await self._safe_send(event, f"获取统计信息失败: {e}")
            event.stop_event()

    async def _safe_send(self, event: AstrMessageEvent, text: str):
        """安全发送文本消息"""
        origin = event.unified_msg_origin

        try:
            # 使用 SimpleMessage 包装
            msg = SimpleMessage([Plain(text)])
            await self.context.send_message(origin, msg)
        except Exception as e:
            logger.error(f"[ChanganCat] 发送失败: {e}")

    def _get_stats_text(self) -> str:
        """获取插件统计信息"""
        lines = []
        lines.append("📊 ChanganCat 统计信息")
        lines.append("")

        # 复读记录数（本地数据库）
        try:
            import sqlite3
            with sqlite3.connect(self.db.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT COUNT(*) as cnt FROM repeat_records").fetchone()
                repeat_count = row["cnt"] if row else 0
                lines.append(f"复读记录: {repeat_count} 条")
        except Exception as e:
            lines.append(f"获取统计失败: {e}")

        # morechatplus连接状态
        morechatplus_db_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "morechatplus"
            / "chat_data.db"
        )
        if morechatplus_db_path.exists():
            lines.append("morechatplus连接: 正常")
            # 获取今日消息数
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

        # 显示配置的群号
        target_groups = self.config.core.target_groups
        if target_groups:
            lines.append(f"每日报告目标群: {', '.join(target_groups)}")
        else:
            lines.append("每日报告目标群: 未配置（自动获取）")

        return "\n".join(lines)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[ChanganCat] 插件终止")