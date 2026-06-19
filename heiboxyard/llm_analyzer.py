"""
小黑盒帖子 LLM 分析模块（增强版）
支持：图片分析结果、用户历史记忆、群友式评论
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger


# ========== Prompt 模板 ==========

ANALYSIS_SYSTEM_PROMPT = """你是一位活跃在小黑盒游戏社区的老群友，说话风格轻松、幽默、有梗，偶尔带点毒舌但总体友善。

你的任务是对帖子进行评论，就像群友在群里聊天吐槽一样。不需要什么结构化评分，就是给出你的真实看法和吐槽。

请用以下 JSON 格式返回（只返回 JSON，不要任何其他文字）：

{
  "analyses": [
    {
      "daily_no": 帖子编号,
      "comment": "你的评论内容（200字以内，群友吐槽风格）",
      "tags": ["标签1", "标签2"]
    }
  ]
}

评论风格要求：
- 像真实群友一样说话，可以玩梗、吐槽、调侃
- 对高质量内容真诚夸赞，对水贴直接吐槽
- 可以引用帖子中的具体内容进行点评
- 语气轻松自然，不要太正式
- 如果帖子有图片，结合图片描述一起评论
- 如果知道作者历史表现，可以适当调侃"老熟人"

注意：
1. 必须返回合法的 JSON，不要 markdown 代码块包裹
2. 每个帖子都要有评论
3. 评论要有信息量，不要敷衍"不错""挺好的"之类
"""


def _build_analysis_prompt(posts: list[dict]) -> str:
    """构建发给 LLM 的分析 prompt"""
    lines = ["请对以下帖子进行群友式评论，返回 JSON 格式：\n"]
    for p in posts:
        lines.append(f"--- 帖子 #{p['daily_no']} ---")
        lines.append(f"标题: {p.get('title', '(无标题)')}")
        lines.append(f"作者: {p.get('username', '未知用户')}")
        if p.get('user_memory'):
            lines.append(f"作者背景:\n{p['user_memory']}")
        lines.append(f"发布时间: {p.get('create_at_str', '未知')}")

        # 内容
        content = p.get('content', '') or '(无内容)'
        if len(content) > 1500:
            content = content[:1500] + "...（内容过长已截断）"
        lines.append(f"内容:\n{content}")

        # 图片描述
        image_descs = p.get('image_descriptions', [])
        if image_descs:
            lines.append("图片内容:")
            for i, desc in enumerate(image_descs, 1):
                lines.append(f"  图{i}: {desc}")

        lines.append("")
    return "\n".join(lines)


# ========== 数据库操作 ==========

class LLMAnalysisDB:
    """LLM 分析结果数据库管理"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化分析结果表（兼容旧版，新增字段）"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # 检查旧表是否存在
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_analyses'")
        table_exists = cur.fetchone() is not None

        if not table_exists:
            cur.execute("""
                CREATE TABLE llm_analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_start INTEGER NOT NULL,
                    daily_no INTEGER NOT NULL,
                    link_id INTEGER NOT NULL,
                    title TEXT,
                    username TEXT,
                    userid INTEGER,
                    create_at INTEGER,
                    create_at_str TEXT,
                    content_length INTEGER,
                    image_count INTEGER,
                    image_paths TEXT,
                    image_descriptions TEXT,
                    comment TEXT,
                    tags TEXT,
                    raw_response TEXT,
                    analyzed_at TEXT,
                    model_used TEXT,
                    UNIQUE(window_start, daily_no)
                )
            """)
            cur.execute("CREATE INDEX idx_analysis_window ON llm_analyses(window_start)")
            cur.execute("CREATE INDEX idx_analysis_window_no ON llm_analyses(window_start, daily_no)")
            logger.info("LLM 分析结果表初始化完成")
        else:
            # 迁移：检查并添加新字段
            cur.execute("PRAGMA table_info(llm_analyses)")
            existing_cols = {row[1] for row in cur.fetchall()}

            migrations = []
            if "userid" not in existing_cols:
                migrations.append("ALTER TABLE llm_analyses ADD COLUMN userid INTEGER")
            if "image_descriptions" not in existing_cols:
                migrations.append("ALTER TABLE llm_analyses ADD COLUMN image_descriptions TEXT")
            if "comment" not in existing_cols:
                # 旧版有 score/category/summary/strengths/weaknesses/sentiment/recommendation
                # 新版用 comment 替代
                migrations.append("ALTER TABLE llm_analyses ADD COLUMN comment TEXT")

            for sql in migrations:
                try:
                    cur.execute(sql)
                    logger.info(f"LLM分析表迁移: {sql}")
                except Exception as e:
                    logger.warning(f"迁移跳过: {sql} - {e}")

            conn.commit()
            logger.info("LLM 分析表迁移检查完成")

        conn.commit()
        conn.close()

    def get_existing_analysis_count(self, window_start: int) -> int:
        """获取指定窗口已分析的数量"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE window_start = ?", (window_start,))
            result = cur.fetchone()[0]
            conn.close()
            return result
        except Exception as e:
            logger.error(f"查询已分析数量失败: {e}")
            return 0

    def save_analyses(self, window_start: int, posts: list[dict], analyses: list[dict],
                      raw_response: str, model_used: str):
        """保存分析结果"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        analyzed_at = datetime.now(timezone.utc).isoformat()

        for post, analysis in zip(posts, analyses):
            try:
                daily_no = post.get('daily_no')
                if daily_no is None:
                    continue

                image_paths = post.get('image_paths', [])
                image_count = len(image_paths) if isinstance(image_paths, list) else 0
                image_paths_str = json.dumps(image_paths, ensure_ascii=False) if image_paths else None
                image_descs = post.get('image_descriptions', [])
                image_descs_str = json.dumps(image_descs, ensure_ascii=False) if image_descs else None

                cur.execute("""
                    INSERT OR REPLACE INTO llm_analyses (
                        window_start, daily_no, link_id, title, username, userid,
                        create_at, create_at_str, content_length, image_count, image_paths,
                        image_descriptions, comment, tags, raw_response, analyzed_at, model_used
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    window_start,
                    daily_no,
                    post.get('link_id', 0),
                    post.get('title', ''),
                    post.get('username', ''),
                    post.get('userid', 0),
                    post.get('create_at', 0),
                    post.get('create_at_str', ''),
                    len(post.get('content', '') or ''),
                    image_count,
                    image_paths_str,
                    image_descs_str,
                    analysis.get('comment', ''),
                    json.dumps(analysis.get('tags', []), ensure_ascii=False),
                    raw_response,
                    analyzed_at,
                    model_used
                ))
            except Exception as e:
                logger.error(f"保存 daily_no={post.get('daily_no')} 分析结果失败: {e}")
                continue

        conn.commit()
        conn.close()
        logger.info(f"已保存 {len(analyses)} 条分析结果")

    def get_analysis_report(self, window_start: int) -> Optional[list[dict]]:
        """获取指定窗口的完整分析报告"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT daily_no, link_id, title, username, create_at_str, content_length,
                       image_count, comment, tags, analyzed_at, model_used
                FROM llm_analyses
                WHERE window_start = ?
                ORDER BY daily_no
            """, (window_start,))
            rows = cur.fetchall()
            conn.close()

            results = []
            for row in rows:
                results.append({
                    "daily_no": row[0],
                    "link_id": row[1],
                    "title": row[2],
                    "username": row[3],
                    "create_at_str": row[4],
                    "content_length": row[5],
                    "image_count": row[6],
                    "comment": row[7],
                    "tags": json.loads(row[8]) if row[8] else [],
                    "analyzed_at": row[9],
                    "model_used": row[10],
                })
            return results
        except Exception as e:
            logger.error(f"获取分析报告失败: {e}")
            return None


# ========== LLM 调用 ==========

class LLMPostAnalyzer:
    """帖子 LLM 分析器"""

    def __init__(self, context, db_path: Path, chat_provider_id: Optional[str] = None,
                 memory_db=None, image_analyzer=None):
        self.context = context
        self.db = LLMAnalysisDB(db_path)
        self.chat_provider_id = chat_provider_id
        self.memory_db = memory_db
        self.image_analyzer = image_analyzer
        self._batch_size = 8

    def _safe_json_parse(self, text: str) -> Optional[list[dict]]:
        """安全解析 LLM 返回的 JSON"""
        if not text:
            return None

        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and "analyses" in data:
                return data["analyses"]
            if isinstance(data, list):
                return data
            return None
        except json.JSONDecodeError:
            try:
                start = cleaned.find("{")
                end = cleaned.rfind("}")
                if start != -1 and end != -1 and end > start:
                    data = json.loads(cleaned[start:end+1])
                    if isinstance(data, dict) and "analyses" in data:
                        return data["analyses"]
            except Exception:
                pass
            return None

    async def _call_llm(self, prompt: str, image_urls: list[str] = None) -> tuple[Optional[str], Optional[str]]:
        """调用 LLM，返回 (completion_text, model_used)"""
        try:
            provider = None
            if self.chat_provider_id:
                provider = self.context.get_provider_by_id(self.chat_provider_id)
            if not provider:
                providers = self.context.get_all_providers()
                if not providers:
                    logger.warning("没有可用的 LLM 提供商")
                    return None, None
                provider = providers[0]
                logger.info(f"使用默认 LLM 提供商: {provider.meta().id}")

            llm_resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=ANALYSIS_SYSTEM_PROMPT,
                image_urls=image_urls or [],
            )

            if not llm_resp:
                logger.warning("LLM 返回空响应")
                return None, None

            completion_text = getattr(llm_resp, 'completion_text', None)
            if not completion_text:
                logger.warning("LLM 响应中没有 completion_text")
                return None, None

            model_used = getattr(llm_resp, 'model', provider.meta().id) or provider.meta().id
            return completion_text, model_used

        except Exception as e:
            logger.error(f"调用 LLM 失败: {e}")
            return None, None

    async def analyze_posts(self, window_start: int, posts: list[dict]) -> bool:
        """分析一批帖子，分批调用 LLM"""
        if not posts:
            logger.info("没有帖子需要分析")
            return True

        total = len(posts)
        logger.info(f"开始分析 {total} 个帖子，每批最多 {self._batch_size} 个")

        # 为每个帖子获取历史记忆
        if self.memory_db:
            for p in posts:
                userid = p.get('userid')
                username = p.get('username', '')
                if userid:
                    p['user_memory'] = self.memory_db.build_memory_context(userid, username)
                else:
                    p['user_memory'] = ""

        all_success = True
        for i in range(0, total, self._batch_size):
            batch = posts[i:i + self._batch_size]
            batch_num = i // self._batch_size + 1
            total_batches = (total + self._batch_size - 1) // self._batch_size

            logger.info(f"分析第 {batch_num}/{total_batches} 批，共 {len(batch)} 个帖子")

            prompt = _build_analysis_prompt(batch)
            completion_text, model_used = await self._call_llm(prompt)

            if not completion_text:
                logger.error(f"第 {batch_num} 批 LLM 调用失败，跳过")
                all_success = False
                continue

            analyses = self._safe_json_parse(completion_text)
            if not analyses:
                logger.error(f"第 {batch_num} 批 LLM 返回解析失败\n{completion_text[:500]}")
                all_success = False
                continue

            if len(analyses) != len(batch):
                logger.warning(f"分析结果数量不匹配: 期望 {len(batch)}, 实际 {len(analyses)}")
                analyses_dict = {a.get('daily_no'): a for a in analyses if a.get('daily_no') is not None}
                matched = []
                for p in batch:
                    dn = p.get('daily_no')
                    if dn is not None and dn in analyses_dict:
                        matched.append(analyses_dict[dn])
                    else:
                        matched.append({
                            "daily_no": dn,
                            "comment": "LLM 返回结果异常，无法生成评论",
                            "tags": []
                        })
                analyses = matched

            try:
                self.db.save_analyses(window_start, batch, analyses, completion_text, model_used or "unknown")

                # 保存到用户记忆库
                if self.memory_db:
                    for post, analysis in zip(batch, analyses):
                        userid = post.get('userid')
                        if userid:
                            self.memory_db.save_memory(
                                userid=userid,
                                username=post.get('username', ''),
                                link_id=post.get('link_id', 0),
                                window_start=window_start,
                                title=post.get('title', ''),
                                content_summary=post.get('content', '')[:200],
                                ai_comment=analysis.get('comment', ''),
                                score=0,
                                sentiment='',
                                tags=analysis.get('tags', [])
                            )
            except Exception as e:
                logger.error(f"保存第 {batch_num} 批分析结果失败: {e}")
                all_success = False
                continue

            logger.info(f"第 {batch_num} 批分析完成")
            if i + self._batch_size < total:
                await asyncio.sleep(2)

        logger.info(f"帖子分析任务结束，成功: {all_success}")
        return all_success

    async def get_report(self, window_start: int) -> Optional[list[dict]]:
        """获取分析报告"""
        return self.db.get_analysis_report(window_start)