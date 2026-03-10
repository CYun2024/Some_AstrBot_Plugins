"""统计管理模块 - 处理表情包和哈气统计（从morechatplus读取）"""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .database import DatabaseManager
from .plugin_config import PluginConfig


class StatsManager:
    """统计管理器"""

    # 表情包匹配正则 [image:序号:图片ID]
    MEME_PATTERN = re.compile(r'\[image:(\d+):([^\]]+)\]')

    # 引用和at标签匹配
    QUOTE_PATTERN = re.compile(r'<引用:\d+>')
    AT_PATTERN = re.compile(r'\[at:\d+\]')

    def __init__(self, db: DatabaseManager, config: PluginConfig):
        self.db = db
        self.config = config
        self._morechatplus_db_path = None

    def set_morechatplus_db_path(self, path: str):
        """设置morechatplus数据库路径"""
        self._morechatplus_db_path = path

    def _clean_content(self, content: str) -> str:
        """清理内容中的标签（引用、at、image）"""
        if not content:
            return ""

        # 移除引用标签 <引用:xxx>
        content = self.QUOTE_PATTERN.sub('', content)
        # 移除at标签 [at:xxx]
        content = self.AT_PATTERN.sub('', content)
        # 移除image标签 [image:x:id]
        content = self.MEME_PATTERN.sub('', content)
        # 移除其他可能的标签
        content = re.sub(r'<[^>]+>', '', content)  # 其他尖括号标签

        # 规范化空白（多个空格变一个）
        content = re.sub(r'\s+', ' ', content).strip()

        return content

    def _is_single_haqi(self, text: str) -> bool:
        """检查单个文本是否是哈气（无空格）"""
        if not text or text == "":
            return False

        # 必须以"哈"开头
        if not text.startswith("哈"):
            return False

        # 后面只能是感叹号或波浪号（或为空）
        suffix = text[1:]  # 去掉"哈"

        if suffix:
            allowed_chars = set(["！", "!", "~", "～"])
            if not all(c in allowed_chars for c in suffix):
                return False

        return True

    def is_haqi(self, content: str) -> int:
        """检查消息包含几个哈气

        返回：哈气次数（0表示没有）

        逻辑：
        1. 先清理掉引用、at、image等标签
        2. 按空格分割成多个部分
        3. 每个部分检查是否是纯"哈"（±感叹号/波浪号）
        4. 返回总次数

        示例：
        - "哈" → 1次
        - "哈！哈!" → 2次
        - "<引用:123> 哈! [at:456] 哈~" → 2次（清理后"哈! 哈~"）
        - "哈气" → 0次（不是纯哈）
        """
        if not content:
            return 0

        # 先清理标签
        cleaned = self._clean_content(content)

        if not cleaned:
            return 0

        # 按空格分割成多个部分
        parts = cleaned.split()

        # 统计每个部分是否是哈气
        count = 0
        for part in parts:
            if self._is_single_haqi(part):
                count += 1

        return count

    def extract_memes(self, content: str) -> List[Tuple[int, str]]:
        """从消息内容中提取表情包

        Returns:
            List[Tuple[序号, 图片ID]]
        """
        matches = self.MEME_PATTERN.findall(content)
        return [(int(idx), img_id) for idx, img_id in matches]

    def get_day_start_timestamp(self, days_ago: int = 0) -> float:
        """获取某天开始的时间戳"""
        now = datetime.now()
        target_day = now - timedelta(days=days_ago)
        start_of_day = target_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day.timestamp()

    def _get_messages_from_morechatplus(
        self, 
        origin: str, 
        start_time: float, 
        end_time: float = None
    ) -> List[Dict]:
        """从morechatplus数据库获取消息"""
        if not self._morechatplus_db_path:
            logger.warning("[ChanganCat] morechatplus数据库路径未设置")
            return []

        try:
            with sqlite3.connect(self._morechatplus_db_path) as conn:
                conn.row_factory = sqlite3.Row

                if end_time:
                    rows = conn.execute(
                        """SELECT user_id, nickname, content, image_urls 
                           FROM messages 
                           WHERE origin = ? AND timestamp >= ? AND timestamp < ?""",
                        (origin, start_time, end_time)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT user_id, nickname, content, image_urls 
                           FROM messages 
                           WHERE origin = ? AND timestamp >= ?""",
                        (origin, start_time)
                    ).fetchall()

                return [{
                    "user_id": row["user_id"],
                    "nickname": row["nickname"],
                    "content": row["content"] or "",
                    "image_urls": json.loads(row["image_urls"] or "[]")
                } for row in rows]
        except Exception as e:
            logger.error(f"[ChanganCat] 从morechatplus获取消息失败: {e}")
            return []

    def get_yesterday_stats(self, origin: str) -> Dict:
        """获取昨日统计（从morechatplus读取）"""
        yesterday_start = self.get_day_start_timestamp(1)
        yesterday_end = self.get_day_start_timestamp(0)

        messages = self._get_messages_from_morechatplus(origin, yesterday_start, yesterday_end)

        # 统计哈气（现在可能一条消息包含多次哈气）
        haqi_count = {}
        # 统计表情包: {image_id: {count, url}}
        meme_stats = {}

        for msg in messages:
            user_id = msg["user_id"]
            nickname = msg["nickname"]
            content = msg["content"]
            image_urls = msg["image_urls"]

            # 哈气统计（按人）- 支持一条消息多个哈气
            haqi_times = self.is_haqi(content)  # 返回次数
            if haqi_times > 0:
                key = (user_id, nickname)
                haqi_count[key] = haqi_count.get(key, 0) + haqi_times
                logger.debug(f"[ChanganCat] 检测到{haqi_times}次哈气: {nickname}({user_id}) - '{content}'")

            # 表情包统计（按群）
            memes = self.extract_memes(content)
            for idx, img_id in memes:
                # 获取对应序号的URL（idx从1开始）
                url = image_urls[idx - 1] if 0 < idx <= len(image_urls) else ""

                if img_id not in meme_stats:
                    meme_stats[img_id] = {"count": 0, "url": url}
                meme_stats[img_id]["count"] += 1

        # 排序哈气榜
        haqi_ranking = [
            (user_id, nickname, count) 
            for (user_id, nickname), count in sorted(haqi_count.items(), key=lambda x: -x[1])
        ]

        # 排序表情包榜
        top_memes = [
            {"image_id": img_id, "use_count": info["count"], "image_url": info["url"]}
            for img_id, info in sorted(meme_stats.items(), key=lambda x: -x[1]["count"])
        ][:self.config.stats.top_meme_count]

        return {
            "haqi_ranking": haqi_ranking,
            "top_memes": top_memes,
            "date": (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")
        }

    def get_today_meme_stats(self, origin: str) -> List[Dict]:
        """获取今日表情包统计（按发送次数排序）"""
        today_start = self.get_day_start_timestamp(0)

        messages = self._get_messages_from_morechatplus(origin, today_start)

        # 统计表情包: {image_id: {count, url}}
        meme_stats = {}

        for msg in messages:
            content = msg["content"]
            image_urls = msg["image_urls"]

            memes = self.extract_memes(content)
            for idx, img_id in memes:
                url = image_urls[idx - 1] if 0 < idx <= len(image_urls) else ""

                if img_id not in meme_stats:
                    meme_stats[img_id] = {"count": 0, "url": url}
                meme_stats[img_id]["count"] += 1

        # 按发送次数排序
        sorted_memes = [
            {"image_id": img_id, "use_count": info["count"], "image_url": info["url"]}
            for img_id, info in sorted(meme_stats.items(), key=lambda x: -x[1]["count"])
        ]

        return sorted_memes

    def get_weekly_stats(self, origin: str) -> Dict:
        """获取本周统计（7天，从morechatplus读取）"""
        week_start = self.get_day_start_timestamp(7)

        messages = self._get_messages_from_morechatplus(origin, week_start)

        # 哈气周榜
        haqi_count = {}
        for msg in messages:
            haqi_times = self.is_haqi(msg["content"])
            if haqi_times > 0:
                key = (msg["user_id"], msg["nickname"])
                haqi_count[key] = haqi_count.get(key, 0) + haqi_times

        haqi_ranking = [
            (user_id, nickname, count) 
            for (user_id, nickname), count in sorted(haqi_count.items(), key=lambda x: -x[1])
        ]

        return {
            "haqi_ranking": haqi_ranking,
            "start_date": (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d"),
            "end_date": (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")
        }

    def get_today_stats(self, origin: str) -> Dict:
        """获取今日统计（0点到当前时间，从morechatplus读取）"""
        today_start = self.get_day_start_timestamp(0)

        messages = self._get_messages_from_morechatplus(origin, today_start)

        # 统计哈气
        haqi_count = {}
        for msg in messages:
            haqi_times = self.is_haqi(msg["content"])
            if haqi_times > 0:
                key = (msg["user_id"], msg["nickname"])
                haqi_count[key] = haqi_count.get(key, 0) + haqi_times

        haqi_ranking = [
            (user_id, nickname, count) 
            for (user_id, nickname), count in sorted(haqi_count.items(), key=lambda x: -x[1])
        ]

        return {
            "haqi_ranking": haqi_ranking,
            "date": datetime.now().strftime("%Y/%m/%d"),
            "total_messages": len(messages)
        }

    def format_haqi_ranking(self, ranking: List[Tuple[str, str, int]],
                           title: str = "哈气榜") -> str:
        """格式化哈气排行榜"""
        if not ranking:
            return f"{title}：暂无数据"

        lines = [f"{title}："]
        medals = ["🥇", "🥈", "🥉"]

        for i, (user_id, nickname, count) in enumerate(ranking[:10]):
            medal = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{medal} {nickname}（{user_id}）- {count}次")

        return "\n".join(lines)

    def format_daily_report(self, origin: str, group_name: str = "") -> Tuple[str, List[Dict]]:
        """格式化每日报告（从morechatplus获取数据）

        Returns:
            (报告文本, 表情包图片列表)
        """
        stats = self.get_yesterday_stats(origin)
        weekly_stats = self.get_weekly_stats(origin)

        lines = []
        lines.append(f"📊 {stats['date']} 哈气统计榜")
        lines.append("")

        # 群聊信息
        if group_name:
            lines.append(f"群聊：{group_name}")
        else:
            lines.append(f"群聊：{origin}")
        lines.append("")

        # 哈气日榜
        haqi_lines = self.format_haqi_ranking(stats["haqi_ranking"], "📈 哈气日榜")
        lines.append(haqi_lines)
        lines.append("")

        # 哈气周榜
        weekly_haqi_lines = self.format_haqi_ranking(weekly_stats["haqi_ranking"], "📊 哈气周榜")
        lines.append(weekly_haqi_lines)
        lines.append("")

        # 表情包榜
        meme_images = []
        if stats["top_memes"]:
            lines.append("🖼️ 今日表情包榜：")
            for i, meme in enumerate(stats["top_memes"][:3], 1):
                lines.append(f"{i}. 使用次数：{meme['use_count']}")
                if meme["image_url"]:
                    meme_images.append({
                        "url": meme["image_url"],
                        "count": meme["use_count"]
                    })
        else:
            lines.append("🖼️ 今日表情包榜：暂无数据")

        return "\n".join(lines), meme_images

    def format_haqi_command_response(self, origin: str, group_name: str = "") -> str:
        """格式化/哈气榜命令响应（从morechatplus获取数据）"""
        today_stats = self.get_today_stats(origin)
        week_stats = self.get_weekly_stats(origin)

        lines = []
        lines.append(f"📊 {datetime.now().strftime('%Y/%m/%d')} 哈气统计榜")
        lines.append("")

        # 群聊信息
        if group_name:
            lines.append(f"群聊：{group_name}")
        else:
            lines.append(f"群聊：{origin}")
        lines.append("")

        # 今日哈气榜
        today_lines = self.format_haqi_ranking(today_stats["haqi_ranking"], "📈 今日哈气榜")
        lines.append(today_lines)
        lines.append("")

        # 本周哈气榜
        weekly_lines = self.format_haqi_ranking(week_stats["haqi_ranking"], "📊 哈气周榜")
        lines.append(weekly_lines)

        return "\n".join(lines)

    def format_meme_command_response(self, origin: str, group_name: str = "") -> Tuple[str, List[Dict]]:
        """格式化/表情包榜命令响应（从morechatplus获取今日数据）

        Returns:
            (报告文本, 表情包图片列表)
        """
        today_stats = self.get_today_meme_stats(origin)

        lines = []
        lines.append(f"🖼️ {datetime.now().strftime('%Y/%m/%d')} 表情包榜（今日）")
        lines.append("")

        # 群聊信息
        if group_name:
            lines.append(f"群聊：{group_name}")
        else:
            lines.append(f"群聊：{origin}")
        lines.append("")

        if not today_stats:
            lines.append("今日暂无表情包数据~")
            return "\n".join(lines), []

        lines.append(f"今日共发送 {len(today_stats)} 种表情包，以下是发送次数TOP {min(5, len(today_stats))}：")
        lines.append("")

        # 准备图片列表
        meme_images = []

        for i, meme in enumerate(today_stats[:5], 1):
            lines.append(f"{i}. 发送次数：{meme['use_count']} 次")
            if meme["image_url"]:
                meme_images.append({
                    "url": meme["image_url"],
                    "count": meme["use_count"]
                })

        return "\n".join(lines), meme_images

    def cleanup_old_stats(self):
        """清理过期统计数据（现在主要是清理本地复读记录）"""
        deleted = self.db.cleanup_old_records(self.config.database.data_retention_days)
        if deleted > 0:
            logger.info(f"[ChanganCat] 清理了 {deleted} 条过期复读记录")