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
        self._morechatplus_image_cache_db_path = None

    def set_morechatplus_db_path(self, path: str):
        """设置morechatplus数据库路径（聊天数据库）"""
        self._morechatplus_db_path = path
        if path:
            from pathlib import Path
            p = Path(path)
            self._morechatplus_image_cache_db_path = str(p.parent / "image_cache.db")
            logger.info(f"[ChanganCat] 聊天数据库: {path}")
            logger.info(f"[ChanganCat] 图片缓存数据库: {self._morechatplus_image_cache_db_path}")

    def _clean_content(self, content: str) -> str:
        """清理内容中的标签（引用、at、image）"""
        if not content:
            return ""

        content = self.QUOTE_PATTERN.sub('', content)
        content = self.AT_PATTERN.sub('', content)
        content = self.MEME_PATTERN.sub('', content)
        content = re.sub(r'<[^>]+>', '', content)
        content = re.sub(r'\s+', ' ', content).strip()

        return content

    def _count_ha_in_text(self, text: str) -> int:
        """统计文本中包含多少次"哈"

        规则：
        1. 单个"哈"（前后无其他哈）：无论有无标点都算1次
        2. 连续多个"哈"（≥2个）：每个后面都必须有标点（!！或~～），且最后一个必须是！，才算N次
        3. 前面有其他文字的不算（如"别哈"、"长安别哈"）

        返回：哈气次数
        """
        if not text:
            return 0

        count = 0
        i = 0
        n = len(text)

        while i < n:
            if text[i] == '哈':
                # 规则3：检查前面字符，必须是开始/标点/空格/哈，不能是其他文字
                if i > 0 and text[i-1] not in ['!', '！', '~', '～', ' ', '哈']:
                    i += 1
                    continue

                # 收集连续的"哈"序列，记录每个哈后面的标点
                ha_chain = []  # 每个元素：'!' 或 '~' 或 None

                j = i
                while j < n and text[j] == '哈':
                    # 检查这个哈后面是否有标点
                    if j + 1 < n and text[j + 1] in ['!', '！', '~', '～']:
                        # 记录标点类型
                        if text[j + 1] in ['!', '！']:
                            ha_chain.append('!')
                        else:
                            ha_chain.append('~')
                        # 跳过这个哈和所有连续标点（如"哈！！"）
                        j += 2
                        while j < n and text[j] in ['!', '！', '~', '～']:
                            j += 1
                    else:
                        # 这个哈后面没有标点
                        ha_chain.append(None)
                        j += 1

                # 规则判断
                if len(ha_chain) == 1:
                    # 单个哈：算1次（前面已过滤"别哈"情况）
                    count += 1
                else:
                    # 连续多个哈：每个必须有标点，且最后必须是！
                    if all(p is not None for p in ha_chain) and ha_chain[-1] == '!':
                        count += len(ha_chain)
                    # 否则整个序列不算

                i = j  # 跳到序列后继续扫描
            else:
                i += 1

        return count

    def is_haqi(self, content: str) -> int:
        """检查消息包含几个哈气（仅文字部分）

        返回：文字哈气次数（0表示没有）
        """
        if not content:
            return 0

        cleaned = self._clean_content(content)

        if not cleaned:
            return 0

        parts = cleaned.split()
        count = 0

        for part in parts:
            ha_count = self._count_ha_in_text(part)
            if ha_count > 0:
                # 验证是否整个部分都是哈气（没有其他字符）
                temp_text = part
                expected_len = 0
                i = 0
                while i < len(temp_text):
                    if temp_text[i] == '哈':
                        expected_len += 1
                        i += 1
                        while i < len(temp_text) and temp_text[i] in ['!', '！', '~', '～']:
                            expected_len += 1
                            i += 1
                    else:
                        break

                if expected_len == len(part):
                    count += ha_count

        return count

    def extract_memes(self, content: str) -> List[Tuple[int, str]]:
        """从消息内容中提取表情包

        Returns:
            List[Tuple[序号, 图片ID]]
        """
        matches = self.MEME_PATTERN.findall(content)
        return [(int(idx), img_id) for idx, img_id in matches]

    def count_haqi_memes(self, content: str) -> int:
        """统计消息中包含的哈气表情包数量

        Args:
            content: 消息内容

        Returns:
            哈气表情包数量
        """
        if not self.config.stats.haqi_meme_ids:
            return 0

        # 将配置的ID自动添加img_前缀（如果用户没加的话）
        haqi_ids = []
        for hid in self.config.stats.haqi_meme_ids:
            hid = hid.strip()
            if hid:
                # 如果用户没加img_前缀，自动添加
                if not hid.startswith("img_"):
                    hid = f"img_{hid}"
                haqi_ids.append(hid)

        memes = self.extract_memes(content)
        count = 0

        for idx, img_id in memes:
            # 检查图片ID是否完全匹配哈气表情包列表中的某个ID
            if img_id in haqi_ids:
                count += 1

        return count

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
                        """SELECT user_id, nickname, content, image_urls, timestamp 
                           FROM messages 
                           WHERE origin = ? AND timestamp >= ? AND timestamp < ?""",
                        (origin, start_time, end_time)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT user_id, nickname, content, image_urls, timestamp 
                           FROM messages 
                           WHERE origin = ? AND timestamp >= ?""",
                        (origin, start_time)
                    ).fetchall()

                return [{
                    "user_id": row["user_id"],
                    "nickname": row["nickname"],
                    "content": row["content"] or "",
                    "image_urls": json.loads(row["image_urls"] or "[]"),
                    "timestamp": row["timestamp"]
                } for row in rows]
        except Exception as e:
            logger.error(f"[ChanganCat] 从morechatplus获取消息失败: {e}")
            return []

    def _get_image_local_path(self, image_id: str) -> Optional[str]:
        """从morechatplus的image_cache.db获取本地存储路径"""
        if not self._morechatplus_image_cache_db_path:
            return None

        from pathlib import Path
        if not Path(self._morechatplus_image_cache_db_path).exists():
            return None

        try:
            with sqlite3.connect(self._morechatplus_image_cache_db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT local_path FROM image_cache WHERE image_id = ?",
                    (image_id,)
                ).fetchone()
                return row["local_path"] if row else None
        except Exception as e:
            logger.error(f"[ChanganCat] 获取图片本地路径失败: {e}")
            return None

    def get_daily_haqi_stats(self, origin: str, days: int = 7) -> Dict:
        """获取按天分组的哈气统计（最近N天）

        Returns:
            {
                "daily_stats": [
                    {
                        "date": "2026/03/10",
                        "ranking": [(user_id, nickname, text_count, meme_count, total_count), ...]
                    },
                    ...
                ],
                "start_date": "2026/03/10",
                "end_date": "2026/03/16"
            }
        """
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)

        # 获取所有消息
        messages = self._get_messages_from_morechatplus(
            origin, 
            start_time.timestamp(), 
            end_time.timestamp()
        )

        # 按天分组统计
        daily_stats = {}

        for msg in messages:
            # 确定这条消息属于哪一天
            msg_time = datetime.fromtimestamp(msg["timestamp"])
            day_key = msg_time.strftime("%Y/%m/%d")

            if day_key not in daily_stats:
                daily_stats[day_key] = {}

            user_id = msg["user_id"]
            nickname = msg["nickname"]
            content = msg["content"]

            # 统计哈气
            text_haqi = self.is_haqi(content)
            meme_haqi = self.count_haqi_memes(content)

            if text_haqi > 0 or meme_haqi > 0:
                key = (user_id, nickname)
                if key not in daily_stats[day_key]:
                    daily_stats[day_key][key] = {"text": 0, "meme": 0}
                daily_stats[day_key][key]["text"] += text_haqi
                daily_stats[day_key][key]["meme"] += meme_haqi

        # 转换为列表格式并排序
        result = []
        # 按日期排序（从早到晚）
        for day in sorted(daily_stats.keys()):
            day_data = daily_stats[day]
            ranking = []
            for (user_id, nickname), counts in day_data.items():
                text_c = counts["text"]
                meme_c = counts["meme"]
                total_c = text_c + meme_c
                ranking.append((user_id, nickname, text_c, meme_c, total_c))
            # 按总数排序
            ranking.sort(key=lambda x: -x[4])
            result.append({
                "date": day,
                "ranking": ranking
            })

        return {
            "daily_stats": result,
            "start_date": start_time.strftime("%Y/%m/%d"),
            "end_date": end_time.strftime("%Y/%m/%d")
        }

    def format_daily_haqi_report(self, origin: str, group_name: str = "", days: int = 7) -> str:
        """格式化按天分组的哈气周榜报告"""
        stats = self.get_daily_haqi_stats(origin, days)
        daily_stats = stats["daily_stats"]

        lines = []
        lines.append(f"📊 哈气周榜 ({stats['start_date']} ~ {stats['end_date']})")
        lines.append("")

        if group_name:
            lines.append(f"群聊：{group_name}")
        else:
            lines.append(f"群聊：{origin}")
        lines.append("")

        if not daily_stats:
            lines.append("暂无数据~")
            return chr(10).join(lines)

        # 遍历每一天
        for day_data in daily_stats:
            date_str = day_data["date"]
            ranking = day_data["ranking"]

            lines.append(f"📅 {date_str}")

            if not ranking:
                lines.append("  当日无哈气记录")
            else:
                medals = ["🥇", "🥈", "🥉"]
                for i, (user_id, nickname, text_c, meme_c, total_c) in enumerate(ranking[:10]):
                    medal = medals[i] if i < 3 else f"{i + 1}."
                    if meme_c > 0:
                        lines.append(f"  {medal} {nickname} - {total_c}次（{text_c}+{meme_c}）")
                    else:
                        lines.append(f"  {medal} {nickname} - {total_c}次")

                if len(ranking) > 10:
                    lines.append(f"  ... 还有 {len(ranking) - 10} 人")

            lines.append("")

        return chr(10).join(lines)

    def get_yesterday_stats(self, origin: str) -> Dict:
        """获取昨日统计（从morechatplus读取）

        Returns:
            {
                "haqi_ranking": [(user_id, nickname, text_count, meme_count, total_count), ...],
                "top_memes": [...],
                "date": "2024/01/15"
            }
        """
        yesterday_start = self.get_day_start_timestamp(1)
        yesterday_end = self.get_day_start_timestamp(0)

        messages = self._get_messages_from_morechatplus(origin, yesterday_start, yesterday_end)

        # 哈气统计: {(user_id, nickname): {"text": x, "meme": y}}
        haqi_count = {}
        # 表情包统计
        meme_stats = {}

        for msg in messages:
            user_id = msg["user_id"]
            nickname = msg["nickname"]
            content = msg["content"]
            image_urls = msg["image_urls"]

            # 分别统计文字和表情包哈气
            text_haqi = self.is_haqi(content)
            meme_haqi = self.count_haqi_memes(content)

            if text_haqi > 0 or meme_haqi > 0:
                key = (user_id, nickname)
                if key not in haqi_count:
                    haqi_count[key] = {"text": 0, "meme": 0}
                haqi_count[key]["text"] += text_haqi
                haqi_count[key]["meme"] += meme_haqi
                logger.debug(f"[ChanganCat] 检测到哈气: {nickname}({user_id}) - 文字{text_haqi}+表情包{meme_haqi}")

            # 表情包统计
            memes = self.extract_memes(content)
            for idx, img_id in memes:
                url = image_urls[idx - 1] if 0 < idx <= len(image_urls) else ""
                local_path = self._get_image_local_path(img_id)

                if img_id not in meme_stats:
                    meme_stats[img_id] = {"count": 0, "url": url, "local_path": local_path}
                else:
                    if url and not meme_stats[img_id]["url"]:
                        meme_stats[img_id]["url"] = url
                    if local_path and not meme_stats[img_id].get("local_path"):
                        meme_stats[img_id]["local_path"] = local_path

                meme_stats[img_id]["count"] += 1

        # 排序哈气榜，格式: (user_id, nickname, text_count, meme_count, total_count)
        haqi_ranking = []
        for (user_id, nickname), counts in haqi_count.items():
            text_c = counts["text"]
            meme_c = counts["meme"]
            total_c = text_c + meme_c
            haqi_ranking.append((user_id, nickname, text_c, meme_c, total_c))

        # 按总数排序
        haqi_ranking.sort(key=lambda x: -x[4])

        # 排序表情包榜
        top_memes = [
            {"image_id": img_id, "use_count": info["count"], "image_url": info["url"], "local_path": info.get("local_path", "")}
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
        meme_stats = {}

        for msg in messages:
            content = msg["content"]
            image_urls = msg["image_urls"]

            memes = self.extract_memes(content)
            for idx, img_id in memes:
                url = image_urls[idx - 1] if 0 < idx <= len(image_urls) else ""
                local_path = self._get_image_local_path(img_id)

                if img_id not in meme_stats:
                    meme_stats[img_id] = {"count": 0, "url": url, "local_path": local_path}
                else:
                    if url and not meme_stats[img_id]["url"]:
                        meme_stats[img_id]["url"] = url
                    if local_path and not meme_stats[img_id].get("local_path"):
                        meme_stats[img_id]["local_path"] = local_path

                meme_stats[img_id]["count"] += 1

        sorted_memes = [
            {"image_id": img_id, "use_count": info["count"], "image_url": info["url"], "local_path": info.get("local_path", "")}
            for img_id, info in sorted(meme_stats.items(), key=lambda x: -x[1]["count"])
        ]

        return sorted_memes

    def get_weekly_stats(self, origin: str) -> Dict:
        """获取本周统计（7天，从morechatplus读取）"""
        week_start = self.get_day_start_timestamp(7)
        messages = self._get_messages_from_morechatplus(origin, week_start)

        # 哈气周榜（包含表情包哈气）
        haqi_count = {}
        for msg in messages:
            text_haqi = self.is_haqi(msg["content"])
            meme_haqi = self.count_haqi_memes(msg["content"])

            if text_haqi > 0 or meme_haqi > 0:
                key = (msg["user_id"], msg["nickname"])
                if key not in haqi_count:
                    haqi_count[key] = {"text": 0, "meme": 0}
                haqi_count[key]["text"] += text_haqi
                haqi_count[key]["meme"] += meme_haqi

        # 格式: (user_id, nickname, text_count, meme_count, total_count)
        haqi_ranking = []
        for (user_id, nickname), counts in haqi_count.items():
            text_c = counts["text"]
            meme_c = counts["meme"]
            total_c = text_c + meme_c
            haqi_ranking.append((user_id, nickname, text_c, meme_c, total_c))

        haqi_ranking.sort(key=lambda x: -x[4])

        return {
            "haqi_ranking": haqi_ranking,
            "start_date": (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d"),
            "end_date": (datetime.now() - timedelta(days=1)).strftime("%Y/%m/%d")
        }

    def get_today_stats(self, origin: str) -> Dict:
        """获取今日统计（0点到当前时间，从morechatplus读取）"""
        today_start = self.get_day_start_timestamp(0)
        messages = self._get_messages_from_morechatplus(origin, today_start)

        haqi_count = {}
        for msg in messages:
            text_haqi = self.is_haqi(msg["content"])
            meme_haqi = self.count_haqi_memes(msg["content"])

            if text_haqi > 0 or meme_haqi > 0:
                key = (msg["user_id"], msg["nickname"])
                if key not in haqi_count:
                    haqi_count[key] = {"text": 0, "meme": 0}
                haqi_count[key]["text"] += text_haqi
                haqi_count[key]["meme"] += meme_haqi

        # 格式: (user_id, nickname, text_count, meme_count, total_count)
        haqi_ranking = []
        for (user_id, nickname), counts in haqi_count.items():
            text_c = counts["text"]
            meme_c = counts["meme"]
            total_c = text_c + meme_c
            haqi_ranking.append((user_id, nickname, text_c, meme_c, total_c))

        haqi_ranking.sort(key=lambda x: -x[4])

        return {
            "haqi_ranking": haqi_ranking,
            "date": datetime.now().strftime("%Y/%m/%d"),
            "total_messages": len(messages)
        }

    def format_haqi_ranking(self, ranking: List[Tuple[str, str, int, int, int]],
                           title: str = "哈气榜") -> str:
        """格式化哈气排行榜

        Args:
            ranking: [(user_id, nickname, text_count, meme_count, total_count), ...]
        """
        if not ranking:
            return f"{title}：暂无数据"

        lines = [f"{title}："]
        medals = ["🥇", "🥈", "🥉"]

        for i, (user_id, nickname, text_c, meme_c, total_c) in enumerate(ranking[:10]):
            medal = medals[i] if i < 3 else f"{i + 1}."
            # 如果有表情包哈气，显示分解 (文字+表情包)
            if meme_c > 0:
                lines.append(f"{medal} {nickname}（{user_id}）- {total_c}次（{text_c}+{meme_c}）")
            else:
                lines.append(f"{medal} {nickname}（{user_id}）- {total_c}次")

        return chr(10).join(lines)

    def format_daily_report(self, origin: str, group_name: str = "") -> Tuple[str, List[Dict]]:
        """格式化每日报告（从morechatplus获取数据）"""
        stats = self.get_yesterday_stats(origin)
        weekly_stats = self.get_weekly_stats(origin)

        lines = []
        lines.append(f"📊 {stats['date']} 哈气统计榜")
        lines.append("")

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
                img_path = meme.get("local_path") or meme.get("image_url")
                if img_path:
                    meme_images.append({
                        "path": img_path,
                        "count": meme["use_count"],
                        "is_local": bool(meme.get("local_path"))
                    })
        else:
            lines.append("🖼️ 今日表情包榜：暂无数据")

        return chr(10).join(lines), meme_images

    def format_haqi_command_response(self, origin: str, group_name: str = "") -> str:
        """格式化/哈气榜命令响应（从morechatplus获取数据）"""
        today_stats = self.get_today_stats(origin)
        week_stats = self.get_weekly_stats(origin)

        lines = []
        lines.append(f"📊 {datetime.now().strftime('%Y/%m/%d')} 哈气统计榜")
        lines.append("")

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

        # 如果有配置哈气表情包，显示提示
        if self.config.stats.haqi_meme_ids:
            lines.append("")
            lines.append(f"💡 当前有 {len(self.config.stats.haqi_meme_ids)} 个哈气表情包计入统计")

        return chr(10).join(lines)

    def format_meme_command_response(self, origin: str, group_name: str = "") -> Tuple[str, List[Dict]]:
        """格式化/表情包榜命令响应"""
        today_stats = self.get_today_meme_stats(origin)

        lines = []
        lines.append(f"🖼️ {datetime.now().strftime('%Y/%m/%d')} 表情包榜（今日）")
        lines.append("")

        if group_name:
            lines.append(f"群聊：{group_name}")
        else:
            lines.append(f"群聊：{origin}")
        lines.append("")

        if not today_stats:
            lines.append("今日暂无表情包数据~")
            return chr(10).join(lines), []

        lines.append(f"今日共发送 {len(today_stats)} 种表情包/图片，以下是发送次数TOP {min(5, len(today_stats))}：")
        lines.append("")
        lines.append("")

        meme_images = []
        valid_count = 0

        for i, meme in enumerate(today_stats[:5], 1):
            img_path = meme.get("local_path") or meme.get("image_url")
            if img_path:
                valid_count += 1
                lines.append(f"{i}. 发送次数：{meme['use_count']} 次")
                meme_images.append({
                    "path": img_path,
                    "count": meme["use_count"],
                    "is_local": bool(meme.get("local_path"))
                })
            else:
                img_id_short = meme["image_id"][:8] if len(meme["image_id"]) > 8 else meme["image_id"]
                lines.append(f"{i}. 发送次数：{meme['use_count']} 次 (ID:{img_id_short}...无图片)")

        if valid_count == 0 and today_stats:
            lines.append("")
            lines.append("⚠️ 注意：检测到表情包记录但无法获取图片，请检查morechatplus图片缓存配置")

        return chr(10).join(lines), meme_images

    def cleanup_old_stats(self):
        """清理过期统计数据"""
        deleted = self.db.cleanup_old_records(self.config.database.data_retention_days)
        if deleted > 0:
            logger.info(f"[ChanganCat] 清理了 {deleted} 条过期复读记录")

    def get_user_haqi_details(self, origin: str, user_id: str, days: int = 1) -> Dict:
        """获取指定用户的哈气详情

        Args:
            origin: 群origin
            user_id: 用户ID
            days: 查询天数（1=今日，7=七日）

        Returns:
            {
                "nickname": "用户昵称",
                "text_messages": ["原始消息内容1", "原始消息内容2", ...],
                "meme_haqi": {"img_xxx": 次数, "img_yyy": 次数},
                "text_count": 文字哈气次数,
                "meme_count": 表情包哈气次数,
                "total_count": 总次数,
                "start_time": 开始时间戳,
                "end_time": 结束时间戳
            }
        """
        end_time = datetime.now()

        # 修正时间范围计算
        if days == 1:
            # 今日：从今天0点开始
            start_time = end_time.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            # 七日：从N天前的0点开始
            start_time = (end_time - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

        start_timestamp = start_time.timestamp()
        end_timestamp = end_time.timestamp()

        messages = self._get_messages_from_morechatplus(origin, start_timestamp, end_timestamp)

        text_messages = []  # 存储原始消息内容
        meme_haqi_count = {}  # 表情包ID:次数
        text_count = 0
        meme_count = 0
        nickname = ""

        # 构建完整的哈气表情包ID列表（自动添加前缀）
        haqi_ids = []
        for hid in self.config.stats.haqi_meme_ids:
            hid = hid.strip()
            if hid:
                if not hid.startswith("img_"):
                    hid = f"img_{hid}"
                haqi_ids.append(hid)

        for msg in messages:
            if str(msg["user_id"]) != str(user_id):
                continue

            nickname = msg.get("nickname", "")
            content = msg.get("content", "")

            # 检查文字哈气
            haqi_times = self.is_haqi(content)
            if haqi_times > 0:
                text_count += haqi_times
                # 保存原始消息和时间
                clean_msg = re.sub(r'\s+', ' ', content).strip()
                if clean_msg:
                    # 转换时间戳为可读格式
                    msg_time = datetime.fromtimestamp(msg.get("timestamp", 0))
                    time_str = msg_time.strftime("%m-%d %H:%M:%S")  # 显示月-日 时:分:秒
                    text_messages.append({
                        "time": time_str,
                        "content": clean_msg,
                        "timestamp": msg.get("timestamp", 0)
                    })

            # 检查表情包哈气
            memes = self.extract_memes(content)
            for idx, img_id in memes:
                if img_id in haqi_ids:
                    meme_count += 1
                    meme_haqi_count[img_id] = meme_haqi_count.get(img_id, 0) + 1

        return {
            "nickname": nickname or f"用户{user_id}",
            "text_messages": text_messages,
            "meme_haqi": meme_haqi_count,
            "text_count": text_count,
            "meme_count": meme_count,
            "total_count": text_count + meme_count,
            "start_time": start_timestamp,
            "end_time": end_timestamp,
            "days": days
        }