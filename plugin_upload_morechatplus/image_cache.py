"""图片缓存管理模块

提供图片缓存功能：
- 基于URL或MD5的图片唯一标识
- 使用计数跟踪（LRU清理策略）
- 可配置的最大缓存数量
- 识图结果缓存
"""

import hashlib
import json
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
    image_id: str          # 图片唯一ID（MD5或自生成）
    url: str               # 原始URL
    local_path: str        # 本地缓存路径
    vision_result: str     # 识图结果
    use_count: int         # 使用次数
    last_used: float       # 最后使用时间
    created_at: float      # 创建时间


class ImageCacheManager:
    """图片缓存管理器"""

    def __init__(self, db_path: Path, max_cache_size: int = 1000):
        """
        Args:
            db_path: 数据库路径
            max_cache_size: 最大缓存图片数量
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                    use_count INTEGER DEFAULT 1,
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
                CREATE INDEX IF NOT EXISTS idx_image_cache_use_count 
                ON image_cache(use_count)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_cache_last_used 
                ON image_cache(last_used)
            """)
            
            conn.commit()
            logger.info(f"[MoreChatPlus] 图片缓存数据库初始化完成: {self.db_path}")

    def _generate_image_id(self, url: str, content: bytes = None) -> str:
        """生成图片唯一ID
        
        优先使用MD5，如果无法计算则使用URL的hash
        """
        if content:
            # 使用内容MD5
            md5 = hashlib.md5(content).hexdigest()
            return f"img_{md5[:16]}"
        else:
            # 使用URL的hash
            url_hash = hashlib.md5(url.encode()).hexdigest()
            return f"url_{url_hash[:16]}"

    def get_image_id(self, url: str) -> str:
        """获取图片ID（基于URL）"""
        return self._generate_image_id(url)

    def get_or_create_cache(
        self, 
        url: str, 
        local_path: str,
        content: bytes = None
    ) -> Tuple[str, bool]:
        """获取或创建图片缓存
        
        Returns:
            (image_id, 是否已存在)
        """
        image_id = self._generate_image_id(url, content)
        
        with self._lock:
            # 检查内存缓存
            if image_id in self._memory_cache:
                entry = self._memory_cache[image_id]
                entry.use_count += 1
                entry.last_used = time.time()
                self._update_db_entry(entry)
                logger.debug(f"[MoreChatPlus] 图片缓存命中(内存): {image_id}")
                return image_id, True
            
            # 检查数据库
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM image_cache WHERE image_id = ?",
                    (image_id,)
                ).fetchone()
                
                if row:
                    # 缓存命中，更新使用计数
                    entry = ImageCacheEntry(
                        image_id=row["image_id"],
                        url=row["url"],
                        local_path=row["local_path"],
                        vision_result=row["vision_result"],
                        use_count=row["use_count"] + 1,
                        last_used=time.time(),
                        created_at=row["created_at"]
                    )
                    self._memory_cache[image_id] = entry
                    self._update_db_entry(entry)
                    logger.debug(f"[MoreChatPlus] 图片缓存命中(数据库): {image_id}")
                    return image_id, True
                
                # 创建新缓存
                entry = ImageCacheEntry(
                    image_id=image_id,
                    url=url,
                    local_path=local_path,
                    vision_result="",
                    use_count=1,
                    last_used=time.time(),
                    created_at=time.time()
                )
                
                # 检查是否需要清理
                self._maybe_cleanup()
                
                # 保存到内存和数据库
                self._memory_cache[image_id] = entry
                self._save_db_entry(entry)
                
                # 保存URL映射
                conn.execute(
                    "INSERT OR REPLACE INTO url_to_image (url, image_id) VALUES (?, ?)",
                    (url, image_id)
                )
                conn.commit()
                
                logger.info(f"[MoreChatPlus] 创建图片缓存: {image_id}")
                return image_id, False

    def get_vision_result(self, image_id: str) -> Optional[str]:
        """获取识图结果"""
        with self._lock:
            # 检查内存缓存
            if image_id in self._memory_cache:
                return self._memory_cache[image_id].vision_result
            
            # 检查数据库
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

    def _save_db_entry(self, entry: ImageCacheEntry) -> None:
        """保存条目到数据库"""
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO image_cache 
                (image_id, url, local_path, vision_result, use_count, last_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.image_id, entry.url, entry.local_path, entry.vision_result,
                entry.use_count, entry.last_used, entry.created_at
            ))
            conn.commit()

    def _update_db_entry(self, entry: ImageCacheEntry) -> None:
        """更新数据库条目"""
        with self._connect() as conn:
            conn.execute("""
                UPDATE image_cache 
                SET use_count = ?, last_used = ?
                WHERE image_id = ?
            """, (entry.use_count, entry.last_used, entry.image_id))
            conn.commit()

    def _maybe_cleanup(self) -> None:
        """检查并清理缓存（LRU策略：删除使用次数最少的）"""
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) as cnt FROM image_cache").fetchone()["cnt"]
            
            if count >= self.max_cache_size:
                # 获取使用次数最少的条目进行删除
                to_delete = conn.execute("""
                    SELECT image_id FROM image_cache 
                    ORDER BY use_count ASC, last_used ASC 
                    LIMIT ?
                """, (max(1, self.max_cache_size // 10),)).fetchall()
                
                for row in to_delete:
                    image_id = row["image_id"]
                    # 从内存缓存删除
                    if image_id in self._memory_cache:
                        del self._memory_cache[image_id]
                    # 从数据库删除
                    conn.execute("DELETE FROM image_cache WHERE image_id = ?", (image_id,))
                    logger.info(f"[MoreChatPlus] 清理图片缓存: {image_id}")
                
                conn.commit()

    def get_cache_stats(self) -> dict:
        """获取缓存统计信息"""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) as cnt FROM image_cache").fetchone()["cnt"]
            total_uses = conn.execute("SELECT SUM(use_count) as total FROM image_cache").fetchone()["total"] or 0
            with_vision = conn.execute(
                "SELECT COUNT(*) as cnt FROM image_cache WHERE vision_result != ''"
            ).fetchone()["cnt"]
            
            return {
                "total_images": total,
                "total_uses": total_uses,
                "with_vision_result": with_vision,
                "max_cache_size": self.max_cache_size,
                "memory_cached": len(self._memory_cache)
            }

    def set_max_cache_size(self, size: int) -> None:
        """设置最大缓存大小"""
        self.max_cache_size = max(100, size)
        self._maybe_cleanup()

    def clear_cache(self) -> int:
        """清空所有缓存"""
        with self._lock:
            self._memory_cache.clear()
            with self._connect() as conn:
                conn.execute("DELETE FROM image_cache")
                conn.execute("DELETE FROM url_to_image")
                conn.commit()
                logger.info("[MoreChatPlus] 图片缓存已清空")
                return conn.total_changes
        return 0
