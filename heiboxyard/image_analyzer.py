"""
小黑盒帖子图片分析模块
负责：调用视觉模型分析帖子图片内容，支持重试机制
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger


# ========== 图片分析 Prompt ==========

IMAGE_ANALYSIS_PROMPT = """请详细描述这张图片的内容。

如果图片包含文字，请尽量转录其中的文字。
如果图片是游戏截图，请描述游戏场景、角色、装备等关键信息。
如果图片是表情包或梗图，请说明其含义和笑点。
如果图片质量较差或无法辨认，请说明原因。

请用简洁的中文描述，控制在200字以内。"""


class ImageAnalysisDB:
    """图片分析结果数据库管理"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化图片分析结果表"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS image_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                image_url TEXT,
                image_index INTEGER DEFAULT 0,
                description TEXT,
                analyzed_at TEXT,
                model_used TEXT,
                retry_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                error_msg TEXT,
                UNIQUE(link_id, image_path)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_img_link ON image_analyses(link_id)")
        conn.commit()
        conn.close()
        logger.info("图片分析结果表初始化完成")

    def get_pending_images(self, link_ids: list[int]) -> list[dict]:
        """获取需要分析的图片列表"""
        if not link_ids:
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            placeholders = ",".join(["?"] * len(link_ids))
            cur.execute(f"""
                SELECT link_id, image_path, image_index
                FROM image_analyses
                WHERE link_id IN ({placeholders}) AND (status = 'pending' OR status = 'failed')
                ORDER BY link_id, image_index
            """, link_ids)
            rows = cur.fetchall()
            conn.close()
            return [{"link_id": r[0], "image_path": r[1], "image_index": r[2]} for r in rows]
        except Exception as e:
            logger.error(f"获取待分析图片失败: {e}")
            return []

    def register_images(self, link_id: int, image_paths: list[str]):
        """注册帖子图片到分析队列"""
        if not image_paths:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            for idx, img_path in enumerate(image_paths):
                cur.execute("""
                    INSERT OR IGNORE INTO image_analyses (link_id, image_path, image_index, status)
                    VALUES (?, ?, ?, 'pending')
                """, (link_id, img_path, idx))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"注册图片失败 link_id={link_id}: {e}")

    def save_result(self, link_id: int, image_path: str, description: str,
                    model_used: str, status: str = 'success', error_msg: str = None):
        """保存分析结果"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                UPDATE image_analyses
                SET description = ?, analyzed_at = ?, model_used = ?, status = ?, error_msg = ?,
                    retry_count = retry_count + 1
                WHERE link_id = ? AND image_path = ?
            """, (description, datetime.now(timezone.utc).isoformat(), model_used,
                  status, error_msg, link_id, image_path))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"保存图片分析结果失败: {e}")

    def get_descriptions_for_post(self, link_id: int) -> list[str]:
        """获取帖子的所有图片描述"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT description FROM image_analyses
                WHERE link_id = ? AND status = 'success' AND description IS NOT NULL
                ORDER BY image_index
            """, (link_id,))
            rows = cur.fetchall()
            conn.close()
            return [r[0] for r in rows if r[0]]
        except Exception as e:
            logger.error(f"获取图片描述失败 link_id={link_id}: {e}")
            return []


class ImagePostAnalyzer:
    """帖子图片分析器"""

    def __init__(self, context, db_path: Path, vision_provider_id: Optional[str] = None):
        self.context = context
        self.db = ImageAnalysisDB(db_path)
        self.vision_provider_id = vision_provider_id
        self._timeout = 600  # 10分钟超时
        self._max_retries = 3

    async def _call_vision_model(self, image_path: str) -> tuple[Optional[str], Optional[str]]:
        """
        调用视觉模型分析单张图片
        使用 provider.text_chat 的 image_urls 参数传入图片路径
        返回 (description, model_used)
        """
        try:
            # 获取 provider
            provider = None
            if self.vision_provider_id:
                provider = self.context.get_provider_by_id(self.vision_provider_id)
            if not provider:
                providers = self.context.get_all_providers()
                if not providers:
                    logger.warning("没有可用的 LLM 提供商")
                    return None, None
                provider = providers[0]
                logger.info(f"使用默认 Provider 进行图片分析: {provider.meta().id}")

            # 调用 provider.text_chat，通过 image_urls 传入图片路径
            llm_resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=IMAGE_ANALYSIS_PROMPT,
                    image_urls=[image_path],  # 支持文件路径
                    system_prompt="你是一位专业的图片内容分析师。",
                ),
                timeout=self._timeout
            )

            if not llm_resp:
                return None, None

            completion_text = getattr(llm_resp, 'completion_text', None)
            model_used = getattr(llm_resp, 'model', provider.meta().id) or provider.meta().id
            return completion_text, model_used

        except asyncio.TimeoutError:
            logger.error(f"图片分析超时: {image_path}")
            return None, None
        except Exception as e:
            logger.error(f"调用视觉模型失败: {e}")
            return None, None

    async def analyze_images(self, link_id: int, image_paths: list[str]) -> list[str]:
        """
        分析帖子的所有图片（串行，一张结束再下一张）
        失败时重试，最多3次
        """
        if not image_paths:
            return []

        # 注册图片到数据库
        self.db.register_images(link_id, image_paths)

        # 限制最多分析 5 张图片
        image_paths = image_paths[:5]
        if len(image_paths) < len(image_paths):
            logger.info(f"图片数量超过5张，仅分析前5张")

        descriptions = []
        for idx, img_path in enumerate(image_paths):
            p = Path(img_path)
            if not p.exists():
                logger.warning(f"图片文件不存在: {img_path}")
                self.db.save_result(link_id, img_path, None, None, 'failed', '文件不存在')
                continue

            logger.info(f"分析图片 {idx+1}/{len(image_paths)}: {img_path}")

            description = None
            model_used = None

            for attempt in range(1, self._max_retries + 1):
                logger.info(f"  尝试 {attempt}/{self._max_retries}")
                description, model_used = await self._call_vision_model(img_path)
                if description:
                    break
                if attempt < self._max_retries:
                    await asyncio.sleep(2)  # 重试前等待

            if description:
                self.db.save_result(link_id, img_path, description, model_used, 'success')
                descriptions.append(description)
                logger.info(f"  ✅ 分析成功")
            else:
                self.db.save_result(link_id, img_path, None, model_used or 'unknown',
                                    'failed', f'重试{self._max_retries}次后仍失败')
                logger.warning(f"  ❌ 分析失败，已重试{self._max_retries}次")

        return descriptions