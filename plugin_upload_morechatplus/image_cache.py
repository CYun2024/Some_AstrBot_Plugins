"""图片缓存管理模块

插件自主管理图片缓存：
- 所有图片保存到插件数据目录
- 基于内容MD5去重（相同内容复用同一文件）
- receive_count: 收到次数统计
- send_count: 使用次数统计
- 清理时物理删除文件
"""

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger


@dataclass
class ImageCacheEntry:
    """图片缓存条目"""
    image_id: str          # 图片唯一ID（基于内容MD5）
    url: str               # 原始URL
    local_path: str        # 本地缓存路径（在插件目录内）
    vision_result: str     # 识图结果
    receive_count: int     # 收到次数
    send_count: int        # 发送/使用次数
    last_used: float       # 最后使用时间
    created_at: float      # 创建时间


class ImageCacheManager:
    """图片缓存管理器 - 插件自主管理"""

    def __init__(self, db_path: Path, images_dir: Path, max_cache_size: int = 500):
        """
        Args:
            db_path: 数据库路径
            images_dir: 图片存储目录（插件自己管理）
            max_cache_size: 最大缓存图片数量
        """
        self.db_path = db_path
        self.images_dir = images_dir
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.max_cache_size = max_cache_size
        self._lock = threading.RLock()
        self._memory_cache: Dict[str, ImageCacheEntry] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._lock, self._connect() as conn:
            # 图片缓存表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS image_cache (
                    image_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    vision_result TEXT DEFAULT '',
                    receive_count INTEGER DEFAULT 1,
                    send_count INTEGER DEFAULT 0,
                    last_used REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

            # URL到image_id的映射表（用于快速查找）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS url_to_image (
                    url TEXT PRIMARY KEY,
                    image_id TEXT NOT NULL
                )
            """)

            # 创建索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_cache_receive_count 
                ON image_cache(receive_count)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_cache_send_count 
                ON image_cache(send_count)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_cache_last_used 
                ON image_cache(last_used)
            """)

            conn.commit()
            logger.info(f"[MoreChatPlus] 图片缓存系统初始化完成: DB={self.db_path}, Images={self.images_dir}")

    def _generate_image_id(self, content: bytes) -> str:
        """基于图片内容生成唯一ID（MD5前16位）"""
        md5 = hashlib.md5(content).hexdigest()
        return f"img_{md5[:16]}"

    def _get_storage_path(self, image_id: str) -> Path:
        """获取图片在插件目录中的存储路径"""
        # 使用子目录分散文件，避免单目录文件过多
        # 例如 img_c72430bdf422415b -> images/c7/2430bdf422415b.jpg
        prefix = image_id[4:6] if len(image_id) > 6 else "00"
        subdir = self.images_dir / prefix
        subdir.mkdir(exist_ok=True)
        return subdir / f"{image_id[4:]}.jpg"

    def save_image(
        self,
        url: str,
        source_path: str,  # 原始下载路径（AstrBot临时文件）
    ) -> Tuple[str, bool, str]:
        """保存图片到插件缓存（基于MD5去重）

        Args:
            url: 图片URL
            source_path: 原始文件路径（将被复制或删除）

        Returns:
            Tuple[image_id, 是否已存在, final_local_path]
            - 如果图片已存在：删除source_path，复用旧文件
            - 如果是新图片：复制到插件目录，保存元数据
        """
        try:
            # 读取文件内容计算MD5
            with open(source_path, 'rb') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"[MoreChatPlus] 读取源图片失败: {e}")
            # 回退到URL-based ID
            url_hash = hashlib.md5(url.encode()).hexdigest()
            image_id = f"url_{url_hash[:16]}"
            return image_id, False, source_path

        image_id = self._generate_image_id(content)
        storage_path = self._get_storage_path(image_id)

        with self._lock:
            # 检查是否已存在（基于MD5的image_id）
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM image_cache WHERE image_id = ?",
                    (image_id,)
                ).fetchone()

                if row:
                    # 图片已存在（内容相同）
                    existing_path = row["local_path"]

                    # 更新计数器
                    conn.execute(
                        "UPDATE image_cache SET receive_count = receive_count + 1, last_used = ? WHERE image_id = ?",
                        (time.time(), image_id)
                    )

                    # 添加URL映射（如果不同）
                    conn.execute(
                        "INSERT OR REPLACE INTO url_to_image (url, image_id) VALUES (?, ?)",
                        (url, image_id)
                    )
                    conn.commit()

                    # 更新内存缓存
                    if image_id in self._memory_cache:
                        entry = self._memory_cache[image_id]
                        entry.receive_count += 1
                        entry.last_used = time.time()

                    # 删除新的临时文件（因为内容重复）
                    try:
                        if os.path.exists(source_path) and source_path != existing_path:
                            os.remove(source_path)
                            logger.debug(f"[MoreChatPlus] 删除重复图片文件: {source_path}")
                    except Exception as e:
                        logger.debug(f"[MoreChatPlus] 删除重复文件失败: {e}")

                    logger.info(f"[MoreChatPlus] 图片已存在(MD5匹配): {image_id}, receive_count+1")
                    return image_id, True, existing_path

                # 新图片，保存到插件目录
                try:
                    # 确保目录存在
                    storage_path.parent.mkdir(parents=True, exist_ok=True)
                    # 复制文件到插件目录
                    shutil.copy2(source_path, storage_path)
                    # 删除原始临时文件
                    if os.path.exists(source_path):
                        os.remove(source_path)

                    logger.info(f"[MoreChatPlus] 新图片已保存: {image_id} -> {storage_path}")

                except Exception as e:
                    logger.error(f"[MoreChatPlus] 保存图片文件失败: {e}")
                    # 回退到原始路径
                    return image_id, False, source_path

                # 检查是否需要清理
                self._maybe_cleanup()

                # 创建数据库记录
                entry = ImageCacheEntry(
                    image_id=image_id,
                    url=url,
                    local_path=str(storage_path),
                    vision_result="",
                    receive_count=1,
                    send_count=0,
                    last_used=time.time(),
                    created_at=time.time()
                )

                conn.execute("""
                    INSERT INTO image_cache 
                    (image_id, url, local_path, vision_result, receive_count, send_count, last_used, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry.image_id, entry.url, entry.local_path, entry.vision_result,
                    entry.receive_count, entry.send_count, entry.last_used, entry.created_at
                ))

                conn.execute(
                    "INSERT OR REPLACE INTO url_to_image (url, image_id) VALUES (?, ?)",
                    (url, image_id)
                )
                conn.commit()

                # 添加到内存缓存
                self._memory_cache[image_id] = entry

                return image_id, False, str(storage_path)

    def get_vision_result(self, image_id: str) -> Optional[str]:
        """获取识图结果"""
        with self._lock:
            if image_id in self._memory_cache:
                return self._memory_cache[image_id].vision_result

            with self._connect() as conn:
                row = conn.execute(
                    "SELECT vision_result FROM image_cache WHERE image_id = ?",
                    (image_id,)
                ).fetchone()
                return row["vision_result"] if row else None

    def set_vision_result(self, image_id: str, vision_result: str) -> bool:
        """设置识图结果"""
        with self._lock:
            if image_id in self._memory_cache:
                self._memory_cache[image_id].vision_result = vision_result

            with self._connect() as conn:
                conn.execute(
                    "UPDATE image_cache SET vision_result = ? WHERE image_id = ?",
                    (vision_result, image_id)
                )
                conn.commit()
                return True
        return False

    def get_local_path(self, image_id: str) -> Optional[str]:
        """获取本地缓存路径"""
        with self._lock:
            if image_id in self._memory_cache:
                return self._memory_cache[image_id].local_path

            with self._connect() as conn:
                row = conn.execute(
                    "SELECT local_path FROM image_cache WHERE image_id = ?",
                    (image_id,)
                ).fetchone()
                return row["local_path"] if row else None

    def lookup_by_url(self, url: str) -> Optional[str]:
        """通过URL查找图片ID"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT image_id FROM url_to_image WHERE url = ?",
                (url,)
            ).fetchone()
            return row["image_id"] if row else None

    def increment_send_count(self, image_id: str) -> None:
        """增加发送计数（当图片被使用/发送时调用）"""
        with self._lock:
            if image_id in self._memory_cache:
                self._memory_cache[image_id].send_count += 1
                self._memory_cache[image_id].last_used = time.time()

            with self._connect() as conn:
                conn.execute(
                    "UPDATE image_cache SET send_count = send_count + 1, last_used = ? WHERE image_id = ?",
                    (time.time(), image_id)
                )
                conn.commit()

    def _maybe_cleanup(self) -> None:
        """检查并清理缓存（删除receive_count最少40% 或 send_count<5 的）"""
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) as cnt FROM image_cache").fetchone()["cnt"]

            if count >= self.max_cache_size:
                # 计算要删除的数量（40%）
                delete_count = int(self.max_cache_size * 0.4)
                delete_count = max(1, delete_count)

                # 获取receive_count最少的40%的图片
                least_received = conn.execute(
                    "SELECT image_id, local_path FROM image_cache ORDER BY receive_count ASC LIMIT ?",
                    (delete_count,)
                ).fetchall()

                # 获取send_count < 5的图片
                low_send = conn.execute(
                    "SELECT image_id, local_path FROM image_cache WHERE send_count < 5"
                ).fetchall()

                # 合并要删除的ID（去重）
                to_delete = {}
                for row in least_received:
                    to_delete[row["image_id"]] = row["local_path"]
                for row in low_send:
                    to_delete[row["image_id"]] = row["local_path"]

                # 限制最多删除的数量（防止误删过多）
                if len(to_delete) > delete_count * 2:
                    # 如果太多，优先删除receive_count少的
                    to_delete = {}
                    for row in least_received:
                        to_delete[row["image_id"]] = row["local_path"]
                    # 补充一些send_count<5的直到达到delete_count*2
                    for row in low_send:
                        if len(to_delete) >= delete_count * 2:
                            break
                        to_delete[row["image_id"]] = row["local_path"]

                deleted = 0
                for image_id, local_path in to_delete.items():
                    # 从内存缓存删除
                    if image_id in self._memory_cache:
                        del self._memory_cache[image_id]

                    # 物理删除文件
                    try:
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            logger.debug(f"[MoreChatPlus] 删除图片文件: {local_path}")
                    except Exception as e:
                        logger.warning(f"[MoreChatPlus] 删除图片文件失败 {local_path}: {e}")

                    # 从数据库删除
                    conn.execute("DELETE FROM image_cache WHERE image_id = ?", (image_id,))
                    conn.execute("DELETE FROM url_to_image WHERE image_id = ?", (image_id,))
                    deleted += 1

                conn.commit()
                logger.info(f"[MoreChatPlus] 清理图片缓存: 删除 {deleted} 张图片 (receive_count最少40% 或 send_count<5)")

    def get_cache_stats(self) -> dict:
        """获取缓存统计信息"""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) as cnt FROM image_cache").fetchone()["cnt"]
            total_receive = conn.execute("SELECT SUM(receive_count) as total FROM image_cache").fetchone()["total"] or 0
            total_send = conn.execute("SELECT SUM(send_count) as total FROM image_cache").fetchone()["total"] or 0
            with_vision = conn.execute(
                "SELECT COUNT(*) as cnt FROM image_cache WHERE vision_result != ''"
            ).fetchone()["cnt"]
            low_send = conn.execute(
                "SELECT COUNT(*) as cnt FROM image_cache WHERE send_count < 5"
            ).fetchone()["cnt"]

            # 计算图片目录实际大小
            total_size = 0
            try:
                for dirpath, dirnames, filenames in os.walk(self.images_dir):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        if os.path.isfile(fp):
                            total_size += os.path.getsize(fp)
            except:
                pass

            return {
                "total_images": total,
                "total_receive_count": total_receive,
                "total_send_count": total_send,
                "with_vision_result": with_vision,
                "send_count_less_than_5": low_send,
                "max_cache_size": self.max_cache_size,
                "memory_cached": len(self._memory_cache),
                "storage_dir": str(self.images_dir),
                "total_size_mb": round(total_size / 1024 / 1024, 2)
            }

    def set_max_cache_size(self, size: int) -> None:
        """设置最大缓存大小"""
        self.max_cache_size = max(100, size)
        self._maybe_cleanup()

    def clear_cache(self) -> int:
        """清空所有缓存（物理删除文件）"""
        with self._lock:
            # 获取所有文件路径
            with self._connect() as conn:
                rows = conn.execute("SELECT local_path FROM image_cache").fetchall()

                # 删除物理文件
                for row in rows:
                    try:
                        if os.path.exists(row["local_path"]):
                            os.remove(row["local_path"])
                    except:
                        pass

                # 删除数据库记录
                conn.execute("DELETE FROM image_cache")
                conn.execute("DELETE FROM url_to_image")
                conn.commit()

            self._memory_cache.clear()
            logger.info("[MoreChatPlus] 图片缓存已清空（包含物理文件）")
            return len(rows)