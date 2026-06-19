"""imagestats/main.py"""

"""ImageStatsPlugin - 图片消息统计与永久存储插件

功能：
1. 启动时全量统计MoreChatPlus数据库中的图片消息
2. 按图片ID聚合计数，记录首次/最后出现时间
3. 对出现次数>5的图片，从MoreChatPlus缓存复制到永久存储
4. 提供/重新统计 和 /总计数 指令
"""

import asyncio
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


# ==================== 正则表达式 ====================

# 匹配消息内容中的图片标记 [image:序号:图片ID]
IMAGE_TAG_RE = re.compile(r'\[image:(\d+):([^\]]+)\]')

# ==================== 数据类 ====================

@dataclass
class ImageCountRecord:
    """图片计数记录"""
    image_id: str
    count: int
    first_seen: float
    last_seen: float
    is_stored: bool
    local_path: Optional[str]
    source_origin: str
    message_ids: List[str]


@dataclass
class StatsTask:
    """统计任务记录"""
    id: int
    task_type: str
    started_at: float
    completed_at: Optional[float]
    total_images: int
    stored_images: int
    status: str


# ==================== 数据库管理器 ====================

class ImageStatsDatabase:
    """图片统计数据库管理器"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._lock, self._connect() as conn:
            # 图片计数表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS image_counts (
                    image_id TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    is_stored INTEGER DEFAULT 0,
                    local_path TEXT,
                    source_origin TEXT,
                    message_ids TEXT DEFAULT '[]'
                )
            """)

            # 统计任务记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stats_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    total_images INTEGER DEFAULT 0,
                    stored_images INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'running'
                )
            """)

            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_counts_count 
                ON image_counts(count DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_counts_stored 
                ON image_counts(is_stored)
            """)

            conn.commit()
            logger.info(f"[ImageStats] 数据库初始化完成: {self.db_path}")

    # ==================== 图片计数操作 ====================

    def save_image_count(self, image_id: str, count: int, first_seen: float,
                         last_seen: float, source_origin: str, message_ids: List[str]) -> bool:
        """保存或更新图片计数"""
        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    INSERT INTO image_counts 
                    (image_id, count, first_seen, last_seen, source_origin, message_ids)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(image_id) DO UPDATE SET
                        count = excluded.count,
                        last_seen = excluded.last_seen,
                        message_ids = excluded.message_ids
                """, (image_id, count, first_seen, last_seen, source_origin,
                      json.dumps(message_ids, ensure_ascii=False)))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ImageStats] 保存图片计数失败 {image_id}: {e}")
            return False

    def get_image_count(self, image_id: str) -> Optional[ImageCountRecord]:
        """获取单条图片计数记录"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM image_counts WHERE image_id = ?",
                    (image_id,)
                ).fetchone()

                if row:
                    return ImageCountRecord(
                        image_id=row["image_id"],
                        count=row["count"],
                        first_seen=row["first_seen"],
                        last_seen=row["last_seen"],
                        is_stored=bool(row["is_stored"]),
                        local_path=row["local_path"],
                        source_origin=row["source_origin"],
                        message_ids=json.loads(row["message_ids"] or "[]")
                    )
                return None
        except Exception as e:
            logger.error(f"[ImageStats] 获取图片计数失败 {image_id}: {e}")
            return None

    def get_top_images(self, limit: int = 5) -> List[ImageCountRecord]:
        """获取计数最高的图片"""
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("""
                    SELECT * FROM image_counts 
                    ORDER BY count DESC 
                    LIMIT ?
                """, (limit,)).fetchall()

                return [
                    ImageCountRecord(
                        image_id=row["image_id"],
                        count=row["count"],
                        first_seen=row["first_seen"],
                        last_seen=row["last_seen"],
                        is_stored=bool(row["is_stored"]),
                        local_path=row["local_path"],
                        source_origin=row["source_origin"],
                        message_ids=json.loads(row["message_ids"] or "[]")
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"[ImageStats] 获取Top图片失败: {e}")
            return []

    def mark_as_stored(self, image_id: str, local_path: str) -> bool:
        """标记图片为已永久存储"""
        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    UPDATE image_counts 
                    SET is_stored = 1, local_path = ? 
                    WHERE image_id = ?
                """, (local_path, image_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ImageStats] 标记存储状态失败 {image_id}: {e}")
            return False

    def clear_all_counts(self) -> int:
        """清空所有计数数据"""
        try:
            with self._lock, self._connect() as conn:
                cursor = conn.execute("DELETE FROM image_counts")
                conn.commit()
                deleted = cursor.rowcount
                logger.info(f"[ImageStats] 已清空 {deleted} 条图片计数记录")
                return deleted
        except Exception as e:
            logger.error(f"[ImageStats] 清空计数数据失败: {e}")
            return 0

    # ==================== 任务记录操作 ====================

    def create_task(self, task_type: str) -> int:
        """创建新任务记录"""
        try:
            with self._lock, self._connect() as conn:
                cursor = conn.execute("""
                    INSERT INTO stats_tasks (task_type, started_at)
                    VALUES (?, ?)
                """, (task_type, time.time()))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"[ImageStats] 创建任务记录失败: {e}")
            return -1

    def complete_task(self, task_id: int, total_images: int, stored_images: int,
                      status: str = "completed") -> bool:
        """完成任务记录"""
        try:
            with self._lock, self._connect() as conn:
                conn.execute("""
                    UPDATE stats_tasks 
                    SET completed_at = ?, total_images = ?, 
                        stored_images = ?, status = ?
                    WHERE id = ?
                """, (time.time(), total_images, stored_images, status, task_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[ImageStats] 完成任务记录失败 {task_id}: {e}")
            return False

    def get_running_task(self) -> Optional[StatsTask]:
        """获取正在运行的任务"""
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute("""
                    SELECT * FROM stats_tasks 
                    WHERE status = 'running'
                    ORDER BY started_at DESC LIMIT 1
                """).fetchone()

                if row:
                    return StatsTask(
                        id=row["id"],
                        task_type=row["task_type"],
                        started_at=row["started_at"],
                        completed_at=row["completed_at"],
                        total_images=row["total_images"],
                        stored_images=row["stored_images"],
                        status=row["status"]
                    )
                return None
        except Exception as e:
            logger.error(f"[ImageStats] 获取运行中任务失败: {e}")
            return None


# ==================== MoreChatPlus 数据库访问器 ====================

class MoreChatPlusAccessor:
    """访问MoreChatPlus数据库的只读封装"""

    def __init__(self, chat_db_path: Path, image_cache_db_path: Path):
        self.chat_db_path = chat_db_path
        self.image_cache_db_path = image_cache_db_path

    def validate(self) -> Tuple[bool, str]:
        """验证数据库是否可用"""
        if not self.chat_db_path.exists():
            return False, f"MoreChatPlus聊天数据库不存在: {self.chat_db_path}"

        try:
            with sqlite3.connect(str(self.chat_db_path)) as conn:
                # 检查必要表
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                table_names = {t[0] for t in tables}

                if "messages" not in table_names:
                    return False, "MoreChatPlus数据库缺少messages表"

                # 检查必要列
                cursor = conn.execute("PRAGMA table_info(messages)")
                columns = {row[1] for row in cursor.fetchall()}
                required = {"content", "image_urls", "timestamp", "has_image",
                           "message_id", "origin"}

                missing = required - columns
                if missing:
                    return False, f"messages表缺少列: {missing}"

            # 检查图片缓存数据库（可选，但建议存在）
            if not self.image_cache_db_path.exists():
                logger.warning(f"[ImageStats] MoreChatPlus图片缓存数据库不存在: {self.image_cache_db_path}")

            return True, ""
        except Exception as e:
            return False, f"验证数据库失败: {e}"

    def stream_image_messages(self, batch_size: int = 1000):
        """流式读取所有含图片的消息（生成器）"""
        conn = sqlite3.connect(str(self.chat_db_path))
        conn.row_factory = sqlite3.Row

        try:
            cursor = conn.execute("""
                SELECT content, image_urls, timestamp, message_id, origin
                FROM messages 
                WHERE has_image = 1 
                ORDER BY timestamp ASC
            """)

            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break

                for row in rows:
                    yield {
                        "content": row["content"] or "",
                        "image_urls": json.loads(row["image_urls"] or "[]"),
                        "timestamp": row["timestamp"],
                        "message_id": str(row["message_id"]),
                        "origin": row["origin"]
                    }
        finally:
            conn.close()

    def get_image_local_path(self, image_id: str) -> Optional[str]:
        """从图片缓存数据库获取本地路径"""
        if not self.image_cache_db_path.exists():
            return None

        try:
            with sqlite3.connect(str(self.image_cache_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT local_path, url FROM image_cache WHERE image_id = ?",
                    (image_id,)
                ).fetchone()

                if row:
                    return row["local_path"] or None
                return None
        except Exception as e:
            logger.error(f"[ImageStats] 查询图片缓存失败 {image_id}: {e}")
            return None


# ==================== 图片存储管理器 ====================

class PermanentImageStorage:
    """永久图片存储管理器"""

    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_storage_path(self, image_id: str) -> Path:
        """获取图片在永久存储目录中的路径"""
        # 分散存储：img_c72430bdf422415b -> permanent/c7/2430bdf422415b.jpg
        prefix = image_id[4:6] if len(image_id) > 6 else "00"
        subdir = self.storage_dir / prefix
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir / f"{image_id[4:]}.jpg"

    def is_already_stored(self, image_id: str) -> bool:
        """检查图片是否已永久存储"""
        path = self._get_storage_path(image_id)
        return path.exists()

    def store_image(self, image_id: str, source_path: str) -> Optional[str]:
        """将图片复制到永久存储目录"""
        if not source_path or not Path(source_path).exists():
            logger.warning(f"[ImageStats] 源图片不存在: {source_path}")
            return None

        dest_path = self._get_storage_path(image_id)

        # 如果已存在，跳过
        if dest_path.exists():
            logger.debug(f"[ImageStats] 图片已存在，跳过: {image_id}")
            return str(dest_path)

        try:
            shutil.copy2(source_path, dest_path)
            logger.info(f"[ImageStats] 图片已永久存储: {image_id} -> {dest_path}")
            return str(dest_path)
        except Exception as e:
            logger.error(f"[ImageStats] 复制图片失败 {image_id}: {e}")
            return None

    def get_storage_path(self, image_id: str) -> Optional[str]:
        """获取已存储图片的路径（如果不存在返回None）"""
        path = self._get_storage_path(image_id)
        return str(path) if path.exists() else None


# ==================== 主插件类 ====================

class ImageStatsPlugin(star.Star):
    """图片消息统计插件"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context

        # 初始化数据目录
        plugin_data_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "imagestats"
        )
        plugin_data_dir.mkdir(parents=True, exist_ok=True)

        # 数据库
        self.db = ImageStatsDatabase(plugin_data_dir / "image_stats.db")

        # 永久存储目录
        self.storage = PermanentImageStorage(plugin_data_dir / "permanent_images")

        # MoreChatPlus数据库路径（自动发现）
        self.mcp_chat_db: Optional[Path] = None
        self.mcp_image_cache_db: Optional[Path] = None
        self.mcp_accessor: Optional[MoreChatPlusAccessor] = None

        # 并发控制
        self._stats_lock = asyncio.Lock()
        self._is_initializing = False

        logger.info("[ImageStats] 插件初始化完成，等待自动发现MoreChatPlus数据库...")

    def _discover_morechatplus(self) -> bool:
        """自动发现MoreChatPlus数据库路径"""
        base_path = Path(get_astrbot_data_path()) / "plugin_data" / "morechatplus"

        chat_db = base_path / "chat_data.db"
        image_cache_db = base_path / "image_cache.db"

        if chat_db.exists():
            self.mcp_chat_db = chat_db
            self.mcp_image_cache_db = image_cache_db

            self.mcp_accessor = MoreChatPlusAccessor(chat_db, image_cache_db)

            valid, msg = self.mcp_accessor.validate()
            if valid:
                logger.info(f"[ImageStats] 成功发现MoreChatPlus数据库: {chat_db}")
                return True
            else:
                logger.error(f"[ImageStats] MoreChatPlus数据库验证失败: {msg}")
                self.mcp_accessor = None
                return False

        logger.warning(f"[ImageStats] 未找到MoreChatPlus数据库: {base_path}")
        return False

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot加载完成后执行初始化统计"""
        await asyncio.sleep(2)  # 等待其他插件完全加载

        if not self._discover_morechatplus():
            logger.error("[ImageStats] 无法发现MoreChatPlus数据库，跳过初始化统计")
            return

        # 后台执行初始化统计
        asyncio.create_task(self._run_stats_task("init"))

    async def _run_stats_task(self, task_type: str) -> StatsTask:
        """执行统计任务（带锁保护）"""
        async with self._stats_lock:
            # 检查是否已有任务在运行
            running = self.db.get_running_task()
            if running:
                logger.warning(f"[ImageStats] 已有统计任务正在运行(ID={running.id})，跳过")
                return running

            self._is_initializing = True
            task_id = self.db.create_task(task_type)

            try:
                logger.info(f"[ImageStats] 开始执行统计任务: {task_type} (ID={task_id})")
                result = await self._perform_stats(task_id)
                return result
            except Exception as e:
                logger.error(f"[ImageStats] 统计任务执行失败: {e}", exc_info=True)
                self.db.complete_task(task_id, 0, 0, "failed")
                raise
            finally:
                self._is_initializing = False

    async def _perform_stats(self, task_id: int) -> StatsTask:
        """执行实际的统计逻辑"""
        if not self.mcp_accessor:
            raise RuntimeError("MoreChatPlus访问器未初始化")

        # 步骤1: 清空现有数据（如果是重新统计）
        # 注意：保留已存储图片的文件，只清空计数表
        cleared = self.db.clear_all_counts()
        logger.info(f"[ImageStats] 已清空 {cleared} 条历史计数记录")

        # 步骤2: 流式统计所有图片消息
        image_stats: Dict[str, Dict] = {}
        total_messages = 0
        total_image_refs = 0

        logger.info("[ImageStats] 开始流式读取消息...")

        for msg in self.mcp_accessor.stream_image_messages(batch_size=1000):
            total_messages += 1
            content = msg["content"]
            image_urls = msg["image_urls"]
            timestamp = msg["timestamp"]
            message_id = msg["message_id"]
            origin = msg["origin"]

            # 解析消息内容中的图片标记
            images = self._extract_images_from_content(content, image_urls)

            for img in images:
                total_image_refs += 1
                img_id = img["image_id"]

                if img_id not in image_stats:
                    image_stats[img_id] = {
                        "count": 0,
                        "first_seen": timestamp,
                        "last_seen": timestamp,
                        "origin": origin,
                        "message_ids": [],
                        "url": img["url"]
                    }

                stats = image_stats[img_id]
                stats["count"] += 1
                stats["last_seen"] = max(stats["last_seen"], timestamp)
                stats["message_ids"].append(message_id)

                # 保留URL（用于可能的下载回退）
                if img["url"] and not stats.get("url"):
                    stats["url"] = img["url"]

            # 每处理1000条消息输出进度
            if total_messages % 1000 == 0:
                logger.info(f"[ImageStats] 已处理 {total_messages} 条消息，"
                           f"发现 {len(image_stats)} 个唯一图片")

        logger.info(f"[ImageStats] 消息处理完成: {total_messages} 条消息, "
                   f"{total_image_refs} 个图片引用, {len(image_stats)} 个唯一图片")

        # 步骤3: 批量写入数据库
        logger.info("[ImageStats] 开始写入计数数据...")
        written = 0
        for img_id, stats in image_stats.items():
            success = self.db.save_image_count(
                image_id=img_id,
                count=stats["count"],
                first_seen=stats["first_seen"],
                last_seen=stats["last_seen"],
                source_origin=stats["origin"],
                message_ids=stats["message_ids"][:100]  # 最多存100个消息ID
            )
            if success:
                written += 1

        logger.info(f"[ImageStats] 已写入 {written}/{len(image_stats)} 条计数记录")

        # 步骤4: 处理高频图片（count > 5）的永久存储
        stored_count = await self._store_high_frequency_images(image_stats)

        # 步骤5: 完成任务
        self.db.complete_task(task_id, len(image_stats), stored_count, "completed")

        logger.info(f"[ImageStats] 统计任务完成: 总计 {len(image_stats)} 个图片, "
                   f"永久存储 {stored_count} 个高频图片")

        return StatsTask(
            id=task_id,
            task_type="init",
            started_at=time.time(),
            completed_at=time.time(),
            total_images=len(image_stats),
            stored_images=stored_count,
            status="completed"
        )

    def _extract_images_from_content(self, content: str,
                                      image_urls: List[str]) -> List[Dict]:
        """从消息内容中提取图片信息"""
        if not content:
            return []

        images = []
        for match in IMAGE_TAG_RE.finditer(content):
            idx = int(match.group(1))
            img_id = match.group(2)

            # 获取对应的URL
            url = ""
            if 0 < idx <= len(image_urls):
                url = image_urls[idx - 1]

            images.append({
                "index": idx,
                "image_id": img_id,
                "url": url
            })

        return images

    async def _store_high_frequency_images(self,
                                           image_stats: Dict[str, Dict]) -> int:
        """存储出现次数>5的图片到永久目录"""
        threshold = 5
        high_freq = {
            img_id: stats for img_id, stats in image_stats.items()
            if stats["count"] > threshold
        }

        if not high_freq:
            logger.info("[ImageStats] 没有高频图片需要存储")
            return 0

        logger.info(f"[ImageStats] 发现 {len(high_freq)} 个高频图片(>{threshold}次)，"
                   "开始永久存储...")

        stored = 0

        # 使用线程池执行文件IO，避免阻塞事件循环
        loop = asyncio.get_event_loop()

        for img_id, stats in high_freq.items():
            # 检查是否已存储
            if self.storage.is_already_stored(img_id):
                # 更新数据库状态
                path = self.storage.get_storage_path(img_id)
                self.db.mark_as_stored(img_id, path)
                logger.debug(f"[ImageStats] 图片已存储，跳过: {img_id}")
                continue

            # 从MoreChatPlus缓存获取源路径
            source_path = None
            if self.mcp_accessor:
                source_path = self.mcp_accessor.get_image_local_path(img_id)

            if not source_path or not Path(source_path).exists():
                logger.warning(f"[ImageStats] 无法找到图片源文件: {img_id}, "
                              f"URL: {stats.get('url', '无')}")
                continue

            # 执行复制（在线程池中）
            try:
                dest_path = await loop.run_in_executor(
                    None,  # 使用默认线程池
                    self.storage.store_image,
                    img_id,
                    source_path
                )

                if dest_path:
                    self.db.mark_as_stored(img_id, dest_path)
                    stored += 1

            except Exception as e:
                logger.error(f"[ImageStats] 存储图片失败 {img_id}: {e}")

        logger.info(f"[ImageStats] 高频图片存储完成: {stored}/{len(high_freq)} 个成功")
        return stored

    # ==================== 指令处理 ====================

    @filter.command("重新统计")
    async def cmd_rebuild_stats(self, event: AstrMessageEvent):
        """重新执行全量统计（清空现有数据，重新扫描）"""
        # 检查MoreChatPlus是否可用
        if not self.mcp_accessor:
            if not self._discover_morechatplus():
                yield event.plain_result(
                    "[ImageStats] ❌ 错误：无法连接到MoreChatPlus数据库，"
                    "请确保MoreChatPlus插件已正确安装并运行"
                )
                return

        # 检查是否有任务在运行
        running = self.db.get_running_task()
        if running:
            yield event.plain_result(
                f"[ImageStats] ⏳ 已有统计任务正在运行(ID={running.id})，"
                f"开始于 {datetime.fromtimestamp(running.started_at).strftime('%H:%M:%S')}"
            )
            return

        yield event.plain_result(
            "[ImageStats] 🔄 开始重新统计，这可能需要一些时间...\n"
            "后台处理中，完成后可通过 /总计数 查看结果"
        )

        # 后台执行
        asyncio.create_task(self._run_stats_task("rebuild"))

    @filter.command("总计数")
    async def cmd_top_images(self, event: AstrMessageEvent):
        """输出计数最高的5张图片"""
        top_images = self.db.get_top_images(limit=5)

        if not top_images:
            yield event.plain_result(
                "[ImageStats] 📊 暂无图片统计数据\n"
                "请等待初始化完成或使用 /重新统计 手动触发"
            )
            return

        lines = [
            f"📊 图片总计数榜（Top {len(top_images)}）",
            f"统计时间：{datetime.now().strftime('%Y/%m/%d %H:%M:%S')}",
            ""
        ]

        for i, img in enumerate(top_images, 1):
            first_time = datetime.fromtimestamp(img.first_seen)
            first_str = first_time.strftime("%Y/%m/%d %H:%M:%S")

            lines.append(f"{i}. ID: `{img.image_id}`")
            lines.append(f"   出现次数：{img.count} 次")
            lines.append(f"   首次出现：{first_str}")
            lines.append(f"   来源群：{img.source_origin}")

            if img.is_stored and img.local_path:
                lines.append(f"   存储状态：✅ 已永久存储")
                # 尝试发送图片
                if Path(img.local_path).exists():
                    yield event.plain_result("\n".join(lines))
                    yield event.image_result(img.local_path)
                    lines = []  # 清空已发送的内容
                else:
                    lines.append(f"   ⚠️ 存储路径失效: {img.local_path}")
            else:
                lines.append(f"   存储状态：❌ 未存储（出现次数≤5或存储失败）")

            lines.append("")  # 空行分隔

        # 发送剩余文本
        if lines:
            yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        """插件终止时清理"""
        logger.info("[ImageStats] 插件终止")
