"""ChanganCat

长安猫插件，提供：
- 表情包统计（每日榜单，从morechatplus读取）
- 哈气统计（日榜、周榜，从morechatplus读取）
- 每日定时统计报告
- 复读功能（从morechatplus读取历史消息）
"""

import asyncio
import re
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

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成时初始化"""
        if self.config.core.enable:
            # 启动定时任务
            asyncio.create_task(self._daily_report_task())
            # 启动清理任务
            asyncio.create_task(self._cleanup_task())
            logger.info(
                f"[ChanganCat] 插件已启用 | "
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
            except Exception as e:
                logger.error(f"[ChanganCat] 清理任务出错: {e}")
                await asyncio.sleep(3600)

    async def _send_daily_reports(self):
        """发送每日报告到所有有记录的群"""
        try:
            # 获取所有有消息的群（从morechatplus数据库）
            origins = self._get_all_origins_from_morechatplus()

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

        # 发送消息
        await self._send_message_to_origin(origin, report_text, meme_images)

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

    async def _send_message_to_origin(self, origin: str, text: str, images: List[dict] = None):
        """发送消息到指定origin"""
        try:
            # 构建消息链
            chain = []
            chain.append(Plain(text))

            # 添加表情包图片
            if images:
                for img_info in images[:3]:  # 最多3张
                    if img_info.get("url"):
                        chain.append(Plain(f"\n\n【表情包TOP{img_info.get('count', '?')}】\n"))
                        chain.append(Image(url=img_info["url"]))

            # 使用 SimpleMessage 包装
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
                        # 生成图片ID（兼容morechatplus格式）
                        img_hash = hash(url) & 0xFFFFFFFF
                        content_parts.append(f"[image:{len(image_urls)}:url_{img_hash:08x}]")
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

            # 构建消息链
            chain = []

            if is_meme and image_urls:
                # 如果是表情包，发送图片
                for url in image_urls[:3]:  # 最多3张
                    if url:
                        chain.append(Image(url=url))
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
            return

        origin = event.unified_msg_origin
        group_name = self._get_group_name(origin)

        try:
            # 生成哈气榜（从morechatplus读取）
            response = self.stats_manager.format_haqi_command_response(origin, group_name)
            await self._safe_send(event, response)
            logger.info(f"[ChanganCat] 已响应/哈气榜命令")
        except Exception as e:
            logger.error(f"[ChanganCat] 哈气榜命令出错: {e}")
            await self._safe_send(event, f"获取哈气榜失败: {e}")

    @filter.command("changancat_stats")
    async def cmd_stats(self, event: AstrMessageEvent):
        """统计信息命令（管理员）"""
        if not self.config.core.enable:
            return

        try:
            # 获取统计信息
            stats_text = self._get_stats_text()
            await self._safe_send(event, stats_text)
        except Exception as e:
            logger.error(f"[ChanganCat] 获取统计信息失败: {e}")
            await self._safe_send(event, f"获取统计信息失败: {e}")

    async def _safe_send(self, event: AstrMessageEvent, text: str):
        """安全发送消息"""
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

        return "\n".join(lines)

    async def terminate(self) -> None:
        """插件终止"""
        logger.info("[ChanganCat] 插件终止")