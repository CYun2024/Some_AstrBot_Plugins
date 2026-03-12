"""复读管理模块 - 处理复读逻辑（从morechatplus数据库读取）"""

import re
import sqlite3
import time
from collections import deque
from typing import Dict, List, Optional

from astrbot.api import logger

from .database import DatabaseManager
from .plugin_config import PluginConfig


class RepeatManager:
    """复读管理器"""

    def __init__(self, db: DatabaseManager, config: PluginConfig):
        self.db = db
        self.config = config
        # 每个群的消息缓存 {origin: deque[(message_id, content, image_urls)]}
        self._message_cache: Dict[str, deque] = {}
        # 表情包匹配正则
        self.MEME_PATTERN = re.compile(r'\[image:(\d+):([^\]]+)\]')
        self._morechatplus_db_path = None

    def set_morechatplus_db_path(self, path: str):
        """设置morechatplus数据库路径"""
        self._morechatplus_db_path = path

    def _get_cache(self, origin: str) -> deque:
        """获取指定群的消息缓存（仅用于本地缓存，非持久化）"""
        if origin not in self._message_cache:
            self._message_cache[origin] = deque(maxlen=self.config.repeat.check_message_count)
        return self._message_cache[origin]

    def _extract_pure_content(self, content: str) -> str:
        """提取纯文本内容（去除at等）"""
        # 移除at标签
        content = re.sub(r'\[at:\d+\]', '', content)
        # 移除引用标签
        content = re.sub(r'<引用:\d+>', '', content)
        # 规范化空白
        content = re.sub(r'\s+', ' ', content).strip()
        return content

    def _is_same_content(self, content1: str, content2: str) -> bool:
        """判断两条消息内容是否相同"""
        # 提取纯内容
        pure1 = self._extract_pure_content(content1)
        pure2 = self._extract_pure_content(content2)

        # 如果都是纯文本，直接比较
        if not self.MEME_PATTERN.search(content1) and not self.MEME_PATTERN.search(content2):
            return pure1 == pure2 and len(pure1) > 0

        # 如果有表情包，需要特殊处理
        # 提取表情包ID列表
        memes1 = self.MEME_PATTERN.findall(content1)
        memes2 = self.MEME_PATTERN.findall(content2)

        # 如果都有表情包，比较表情包
        if memes1 and memes2:
            # 比较表情包ID列表
            ids1 = [img_id for _, img_id in memes1]
            ids2 = [img_id for _, img_id in memes2]
            return ids1 == ids2

        # 一个有表情包一个没表情包，不相同
        return False

    def check_and_record_message(self, origin: str, message_id: str,
                                  user_id: str, content: str,
                                  image_urls: List[str]) -> Optional[Dict]:
        """检查并记录消息，返回需要复读的信息（兼容实时消息和数据库查询）

        Returns:
            {
                "content": 复读内容,
                "image_urls": 图片URL列表,
                "is_meme": 是否是表情包
            } 或 None
        """
        if not self.config.repeat.enable:
            return None

        # 忽略bot自己的消息
        if user_id == self.config.core.bot_qq_id:
            return None

        # 忽略空消息
        pure_content = self._extract_pure_content(content)
        if not pure_content and not self.MEME_PATTERN.search(content):
            return None

        cache = self._get_cache(origin)

        # 添加到缓存
        cache.append({
            "message_id": message_id,
            "user_id": user_id,
            "content": content,
            "image_urls": image_urls,
            "timestamp": time.time()
        })

        # 检查是否需要复读
        return self._check_repeat(origin, cache)

    def _check_repeat(self, origin: str, cache: deque) -> Optional[Dict]:
        """检查是否需要复读"""
        if len(cache) < self.config.repeat.repeat_threshold:
            return None

        # 统计最近消息中各内容的出现次数
        content_count: Dict[str, int] = {}
        content_info: Dict[str, Dict] = {}

        for msg in cache:
            content = msg["content"]
            pure = self._extract_pure_content(content)

            # 使用纯内容作为key（对于表情包会特殊处理）
            key = pure if not self.MEME_PATTERN.search(content) else content

            if key not in content_count:
                content_count[key] = 0
                content_info[key] = msg

            content_count[key] += 1

        # 找出出现次数最多的内容
        for key, count in content_count.items():
            if count >= self.config.repeat.repeat_threshold:
                msg = content_info[key]
                content = msg["content"]

                # 检查是否已经复读过
                since = time.time() - 3600  # 1小时内
                if self.db.has_repeated(origin, content, since):
                    logger.debug(f"[ChanganCat] 已复读过该内容，跳过")
                    continue

                # 记录复读
                self.db.record_repeat(origin, content, msg["message_id"])

                # 判断是否是表情包（检查是否有[image:x:xxx]标记）
                is_meme = self.MEME_PATTERN.search(content) is not None

                logger.info(f"[ChanganCat] 触发复读: {content[:50]}...")

                return {
                    "content": content,
                    "image_urls": msg["image_urls"],
                    "is_meme": is_meme,
                    "message_id": msg["message_id"]
                }

        return None

    def check_repeat_from_morechatplus(self, origin: str) -> Optional[Dict]:
        """从morechatplus数据库检查最近消息是否需要复读

        查询最近 check_message_count 条消息，如果有 repeat_threshold 条相同则复读
        """
        if not self.config.repeat.enable or not self._morechatplus_db_path:
            return None

        try:
            import json
            with sqlite3.connect(self._morechatplus_db_path) as conn:
                conn.row_factory = sqlite3.Row
                # 获取最近 check_message_count 条消息
                rows = conn.execute(
                    """SELECT message_id, user_id, content, image_urls 
                       FROM messages 
                       WHERE origin = ? 
                       ORDER BY timestamp DESC 
                       LIMIT ?""",
                    (origin, self.config.repeat.check_message_count)
                ).fetchall()

                if len(rows) < self.config.repeat.repeat_threshold:
                    return None

                # 统计内容
                content_count = {}
                content_info = {}

                for row in rows:
                    user_id = row["user_id"]
                    # 跳过bot自己的消息
                    if user_id == self.config.core.bot_qq_id:
                        continue

                    content = row["content"] or ""
                    pure = self._extract_pure_content(content)

                    if not pure and not self.MEME_PATTERN.search(content):
                        continue

                    key = pure if not self.MEME_PATTERN.search(content) else content

                    if key not in content_count:
                        content_count[key] = 0
                        # 解析image_urls
                        try:
                            img_urls = json.loads(row["image_urls"] or "[]")
                        except:
                            img_urls = []

                        content_info[key] = {
                            "content": content,
                            "image_urls": img_urls,
                            "message_id": row["message_id"],
                            "is_meme": self.MEME_PATTERN.search(content) is not None
                        }

                    content_count[key] += 1

                # 检查是否有达到阈值的
                for key, count in content_count.items():
                    if count >= self.config.repeat.repeat_threshold:
                        info = content_info[key]

                        # 检查是否已经复读过（1小时内）
                        since = time.time() - 3600
                        if self.db.has_repeated(origin, info["content"], since):
                            logger.debug(f"[ChanganCat] 已复读过该内容，跳过")
                            continue

                        # 记录复读
                        self.db.record_repeat(origin, info["content"], info["message_id"])

                        logger.info(f"[ChanganCat] 从数据库触发复读: {info['content'][:50]}...")
                        return info

                return None

        except Exception as e:
            logger.error(f"[ChanganCat] 从morechatplus检查复读失败: {e}")
            return None

    def should_repeat(self, origin: str, content: str, user_id: str) -> bool:
        """检查是否应该复读某条消息（用于外部调用）"""
        if not self.config.repeat.enable:
            return False

        if user_id == self.config.core.bot_qq_id:
            return False

        cache = self._get_cache(origin)

        # 统计相同内容数量
        count = 0
        for msg in cache:
            if self._is_same_content(msg["content"], content):
                count += 1

        return count >= self.config.repeat.repeat_threshold - 1  # -1 because current msg not in cache yet

    def clear_cache(self, origin: str = None):
        """清空消息缓存"""
        if origin:
            if origin in self._message_cache:
                self._message_cache[origin].clear()
        else:
            self._message_cache.clear()