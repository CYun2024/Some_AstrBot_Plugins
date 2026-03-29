"""ChanganCat

长安猫插件，提供：
- 表情包统计（每日榜单，从morechatplus读取）
- 哈气统计（日榜、周榜，从morechatplus读取）
- 每日定时统计报告（支持配置指定群号，带开关）
- 复读功能（修复表情包复读，从morechatplus读取历史消息）
- 个人哈气详情查询（支持图片渲染）
- 哈气周榜（按天分组显示最近7天统计）
- 哈气趋势图（支持自定义时间范围）
"""

import asyncio
import hashlib
import re
import aiohttp
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Tuple

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

# 趋势图生成相关导入
try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.font_manager import FontProperties
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
    logger.warning("[ChanganCat] matplotlib库未安装，趋势图功能将不可用")


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

        # 临时下载目录（用于网络图片和趋势图）
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

    def _setup_matplotlib_font(self):
        """设置matplotlib中文字体"""
        if not MATPLOTLIB_AVAILABLE:
            return False

        try:
            # 尝试常见中文字体
            chinese_fonts = [
                'WenQuanYi Micro Hei',
                'WenQuanYi Zen Hei', 
                'SimHei',
                'Microsoft YaHei',
                'Noto Sans CJK SC',
                'Droid Sans Fallback'
            ]

            # 检查系统中可用的字体
            available_font = None
            from matplotlib import font_manager
            system_fonts = [f.name for f in font_manager.fontManager.ttflist]

            for font in chinese_fonts:
                if font in system_fonts:
                    available_font = font
                    break

            if available_font:
                plt.rcParams['font.sans-serif'] = [available_font, 'DejaVu Sans']
            else:
                # 如果没找到中文字体，使用默认字体并警告
                logger.warning("[ChanganCat] 未找到系统中文字体，趋势图中文可能显示为方块")

            plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题
            return True

        except Exception as e:
            logger.error(f"[ChanganCat] 设置matplotlib字体失败: {e}")
            return False

    def _parse_trend_args(self, args_text: str) -> Dict[str, any]:
        """解析趋势图命令参数

        支持格式：
        - /哈气趋势 @xxx          -> 默认7天
        - /哈气趋势 @xxx 14       -> 最近14天
        - /哈气趋势 @xxx 260310 260312  -> 指定日期范围 YYMMDD
        """
        result = {
            'days': 7,  # 默认7天
            'start_date': None,
            'end_date': None,
            'use_date_range': False
        }

        if not args_text:
            return result

        # 移除@并提取纯数字参数
        parts = args_text.replace('@', '').strip().split()

        # 提取所有6位数字（可能是日期YYMMDD）
        date_candidates = []
        other_numbers = []

        for part in parts:
            if part.isdigit():
                if len(part) == 6:
                    # 可能是 YYMMDD 格式
                    date_candidates.append(part)
                elif len(part) <= 3:
                    # 可能是天数（1-3位数字）
                    other_numbers.append(int(part))

        # 如果有两个6位数字，视为日期范围
        if len(date_candidates) >= 2:
            try:
                # 解析为 YYMMDD -> YYYY/MM/DD
                def parse_yymmdd(yymmdd):
                    yy = int(yymmdd[:2])
                    mm = int(yymmdd[2:4])
                    dd = int(yymmdd[4:6])
                    year = 2000 + yy if yy < 50 else 1900 + yy
                    return f"{year}/{mm:02d}/{dd:02d}"

                result['start_date'] = parse_yymmdd(date_candidates[0])
                result['end_date'] = parse_yymmdd(date_candidates[1])
                result['use_date_range'] = True

                # 确保 start <= end
                if result['start_date'] > result['end_date']:
                    result['start_date'], result['end_date'] = result['end_date'], result['start_date']

            except Exception as e:
                logger.error(f"[ChanganCat] 解析日期参数失败: {e}")
                result['use_date_range'] = False

        # 如果没用日期范围，检查是否有天数参数
        if not result['use_date_range'] and other_numbers:
            days = other_numbers[0]
            if 1 <= days <= 365:
                result['days'] = days

        return result

    def _generate_trend_chart(self, data_records: List, user_id: str, nickname: str, 
                             title_suffix: str = "") -> Optional[str]:
        """生成哈气趋势图（三条折线版本）

        Args:
            data_records: DailyHaqiRecord 列表
            user_id: 用户QQ号
            nickname: 用户昵称
            title_suffix: 标题后缀（如" (近7日)"）

        Returns:
            图片保存路径，失败返回None
        """
        if not MATPLOTLIB_AVAILABLE:
            logger.error("[ChanganCat] matplotlib不可用，无法生成趋势图")
            return None

        if not data_records:
            logger.error("[ChanganCat] 无数据记录，无法生成趋势图")
            return None

        try:
            self._setup_matplotlib_font()

            # 准备数据（按日期排序）
            sorted_records = sorted(data_records, key=lambda x: x.date)
            dates = [datetime.strptime(r.date, "%Y/%m/%d") for r in sorted_records]
            text_counts = [r.text_count for r in sorted_records]
            meme_counts = [r.meme_count for r in sorted_records]
            total_counts = [r.text_count + r.meme_count for r in sorted_records]

            # 创建图表（使用双Y轴）
            fig, ax1 = plt.subplots(figsize=(12, 6), dpi=150)

            # 设置标题
            full_title = f"{nickname}({user_id})的哈气趋势图{title_suffix}"
            ax1.set_title(full_title, fontsize=14, fontweight='bold', pad=20)

            # X轴格式化为 MM-DD
            date_labels = [d.strftime("%m-%d") for d in dates]
            x_pos = range(len(dates))

            # 绘制三条折线
            # 1. 文字哈气（红色）
            line1 = ax1.plot(x_pos, text_counts, 'o-', color='#FF6B6B', 
                           linewidth=2, markersize=6, label='文字哈气', alpha=0.8)

            # 2. 表情包哈气（青色）
            line2 = ax1.plot(x_pos, meme_counts, 's-', color='#4ECDC4', 
                           linewidth=2, markersize=6, label='表情包哈气', alpha=0.8)

            # 3. 总计（黄色/金色，加粗）
            line3 = ax1.plot(x_pos, total_counts, '^-', color='#FFD93D', 
                           linewidth=3, markersize=8, label='总计', alpha=0.9, zorder=5)

            # 在数据点上显示数值（只显示总计的数值避免太拥挤）
            for i, (x, y) in enumerate(zip(x_pos, total_counts)):
                if y > 0:
                    ax1.annotate(f'{int(y)}', (x, y), 
                               textcoords="offset points", xytext=(0, 10),
                               ha='center', fontsize=9, fontweight='bold', color='#FFD93D')

            # 设置X轴
            ax1.set_xlabel('日期', fontsize=12)
            ax1.set_xticks(x_pos)
            ax1.set_xticklabels(date_labels, rotation=45, ha='right')
            ax1.set_ylabel('哈气次数', fontsize=12)

            # 添加网格线
            ax1.grid(True, linestyle='--', alpha=0.3, axis='y')
            ax1.grid(True, linestyle='--', alpha=0.1, axis='x')

            # 合并图例
            lines = line1 + line2 + line3
            labels = [l.get_label() for l in lines]
            ax1.legend(lines, labels, loc='upper left', frameon=True, fontsize=10)

            # 设置边距
            plt.tight_layout()

            # 保存图片
            filename = f"haqi_trend_{user_id}_{int(datetime.now().timestamp())}.png"
            save_path = self.temp_dir / filename

            plt.savefig(save_path, dpi=150, bbox_inches='tight', 
                       facecolor='white', edgecolor='none')
            plt.close(fig)

            logger.info(f"[ChanganCat] 趋势图已生成: {save_path}")
            return str(save_path)

        except Exception as e:
            logger.error(f"[ChanganCat] 生成趋势图失败: {e}", exc_info=True)
            return None

    def _sync_haqi_data(self, origin: str, days: int = 7) -> int:
        """同步哈气数据到数据库

        获取最近N天数据并保存，但只有最近3天的数据会覆盖更新，
        3天以前的数据只插入不更新（保留历史）。

        Args:
            origin: 群origin
            days: 获取多少天的数据（默认7天）

        Returns:
            保存的记录总数
        """
        # 获取最近N天的日期列表
        recent_dates = self._get_recent_dates(days)

        # 获取这些天的详细统计数据
        daily_stats = self.stats_manager.get_haqi_stats_for_dates(origin, recent_dates)

        # 提取 ranking 数据
        ranking_data = {date: data["ranking"] for date, data in daily_stats.items()}

        # 批量保存（使用3天覆盖策略）
        saved_result = self.db.save_daily_haqi_stats_batch(origin, ranking_data, max_override_days=3)

        total_saved = sum(saved_result.values())
        logger.info(f"[ChanganCat] 已同步 {days} 天哈气数据，共 {total_saved} 条记录（仅最近3天覆盖更新）")
        return total_saved

    def _get_recent_dates(self, days: int = 7) -> List[str]:
        """获取最近N天的日期字符串列表（YYYY/MM/DD格式）"""
        dates = []
        today = datetime.now()
        for i in range(days, 0, -1):  # 从今天往前数
            date_obj = today - timedelta(days=i)
            dates.append(date_obj.strftime("%Y/%m/%d"))
        return dates

    def _render_haqi_detail_image(self, data: dict, save_path: str) -> bool:
        """渲染哈气详情为图片（原有方法保持不变）"""
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
        """下载网络图片到本地临时目录（原有方法）"""
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
        """每日报告定时任务（原有方法）"""
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
            real_origin = self._get_real_origin(internal_id)
            if not real_origin:
                logger.error(f"[ChanganCat] 无法发送日报到 {internal_id}：未缓存真实origin")
                return

            group_name = self._get_group_name(internal_id)
            report_text = self.stats_manager.format_haqi_command_response(real_origin, group_name)
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
        """执行复读（原有方法）"""
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

    @filter.command("哈气趋势")
    async def cmd_haqi_trend(self, event: AstrMessageEvent):
        """哈气趋势图命令 - 生成指定用户的哈气趋势图表

        用法：
        /哈气趋势 @xxx          -> 默认显示最近7天
        /哈气趋势 @xxx 14       -> 最近14天
        /哈气趋势 @xxx 260310 260312  -> 指定日期范围 YYMMDD格式
        """
        if not self.config.core.enable:
            return

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await self._safe_send(event, "该命令只能在群聊中使用")
            event.stop_event()
            return

        if not MATPLOTLIB_AVAILABLE:
            await self._safe_send(event, "趋势图功能需要安装matplotlib库")
            event.stop_event()
            return

        # 提取@的目标和参数
        target_id = None
        args_text = ""

        for comp in event.get_messages():
            if isinstance(comp, At):
                target_id = str(comp.qq)
            elif isinstance(comp, Plain):
                args_text = comp.text.replace("/哈气趋势", "").strip()

        if not target_id:
            help_text = """请@想要查看趋势的群友，例如：
/哈气趋势 @张三
也可以指定天数：/哈气趋势 @张三 14
或指定日期：/哈气趋势 @张三 260310 260312"""
            await self._safe_send(event, help_text)
            event.stop_event()
            return

        origin = event.unified_msg_origin
        internal_id = self._extract_internal_id(origin)

        # 解析参数
        args = self._parse_trend_args(args_text)

        try:
            # 获取数据
            if args['use_date_range']:
                # 使用指定日期范围
                records = self.db.get_daily_haqi_stats_range(
                    origin, args['start_date'], args['end_date']
                )
                # 筛选特定用户
                user_records = [r for r in records if r.user_id == target_id]
                title_suffix = f" ({args['start_date']}~{args['end_date']})"
            else:
                # 使用最近N天
                records = self.db.get_user_haqi_trend(origin, target_id, args['days'])
                user_records = records
                title_suffix = f" (近{args['days']}日)"

            if not user_records:
                # 尝试从昵称映射数据库获取昵称
                user_info = self.db.get_user_info(origin, target_id)
                nickname = user_info.nickname if user_info else f"用户{target_id}"
                await self._safe_send(event, f"{nickname} 在选定时间段内没有哈气记录~")
                event.stop_event()
                return

            # 获取用户昵称（优先使用记录中最新的）
            user_records_sorted = sorted(user_records, key=lambda x: x.timestamp, reverse=True)
            nickname = user_records_sorted[0].nickname

            # 生成趋势图
            chart_path = self._generate_trend_chart(
                user_records, target_id, nickname, title_suffix
            )

            if chart_path and Path(chart_path).exists():
                from astrbot.api.message_components import Image as CompImage
                msg = SimpleMessage([CompImage(file=chart_path)])
                await self.context.send_message(event.unified_msg_origin, msg)
                logger.info(f"[ChanganCat] 已发送哈气趋势图 for {target_id}")
            else:
                await self._safe_send(event, "生成趋势图失败，请检查日志")

            event.stop_event()

        except Exception as e:
            logger.error(f"[ChanganCat] 哈气趋势命令出错: {e}", exc_info=True)
            await self._safe_send(event, f"生成趋势图失败: {e}")
            event.stop_event()

    @filter.command("哈气榜")
    async def cmd_haqi_ranking(self, event: AstrMessageEvent):
        """哈气榜命令 - 显示今日数据并同步最近7天数据（仅最近3天覆盖保存）"""
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
            # 显示今日哈气榜
            response = self.stats_manager.format_haqi_command_response(origin, group_name)

            # 同步最近7天数据（仅最近3天覆盖，3天前保留）
            saved_count = self._sync_haqi_data(origin, days=7)
            logger.info(f"[ChanganCat] /哈气榜 已同步7天数据，共 {saved_count} 条记录")

            await self._safe_send(event, response)
            event.stop_event()

        except Exception as e:
            logger.error(f"[ChanganCat] 哈气榜命令出错: {e}")
            await self._safe_send(event, f"获取哈气榜失败: {e}")
            event.stop_event()

    @filter.command("哈气周榜")
    async def cmd_daily_haqi_ranking(self, event: AstrMessageEvent):
        """哈气周榜命令 - 按天显示最近7天的哈气统计，并同步数据（仅最近3天覆盖保存）"""
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
            # 显示最近7天分日哈气榜
            response = self.stats_manager.format_daily_haqi_report(origin, group_name, days=7)

            # 同步最近7天数据（仅最近3天覆盖，3天前保留）
            saved_count = self._sync_haqi_data(origin, days=7)
            logger.info(f"[ChanganCat] /哈气周榜 已同步7天数据，共 {saved_count} 条记录")

            await self._safe_send(event, response)
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

        if internal_id not in self._real_origin_cache:
            self._real_origin_cache[internal_id] = origin
            logger.info(f"[ChanganCat] 测试前已缓存当前群: {internal_id}")

        await self._safe_send(event, "正在测试发送每日哈气榜...")
        logger.info("[ChanganCat] 手动触发每日报告测试")

        try:
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
        """获取插件统计信息（添加数据库统计）"""
        lines = []
        lines.append("📊 ChanganCat 统计信息")
        lines.append("")

        try:
            import sqlite3
            with sqlite3.connect(self.db.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # 复读记录数
                row = conn.execute("SELECT COUNT(*) as cnt FROM repeat_records").fetchone()
                repeat_count = row["cnt"] if row else 0
                lines.append(f"复读记录: {repeat_count} 条")

                # 每日哈气统计记录数（新增）
                row = conn.execute("SELECT COUNT(*) as cnt FROM daily_haqi_stats").fetchone()
                daily_haqi_count = row["cnt"] if row else 0
                lines.append(f"每日哈气记录: {daily_haqi_count} 条")

                # 用户映射记录数（新增）
                row = conn.execute("SELECT COUNT(*) as cnt FROM user_info").fetchone()
                user_info_count = row["cnt"] if row else 0
                lines.append(f"用户映射记录: {user_info_count} 条")

        except Exception as e:
            lines.append(f"获取数据库统计失败: {e}")

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

        lines.append(f"图片渲染(PIL): {'可用' if PIL_AVAILABLE else '不可用'}")
        lines.append(f"趋势图(matplotlib): {'可用' if MATPLOTLIB_AVAILABLE else '不可用'}")

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