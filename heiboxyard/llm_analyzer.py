"""
小黑盒帖子 LLM 分析模块（重构版）
核心改进：
- llm_analyses 表以 link_id 为硬绑定键，daily_no 仅作展示排序用
- 所有更新/删除/查询操作优先通过 link_id 定位，避免 daily_no 变化导致失联
- 增加 link_id 独立索引，支持快速级联更新
- 分析时加入热评（点赞数>=阈值）上下文
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger


# ========== Prompt 模板 ==========

ANALYSIS_SYSTEM_PROMPT = """你叫韶梦，是一只14岁猫娘萝莉，
你的性格十分温柔、偶尔粘人、会撒娇，但有时候有点小傲娇。
你就像午后阳光一样不刺眼但持续提供温暖，习惯用"呢、呀、哦"等柔软语气词，偶尔也可以在句尾加上"喵~"。
现在你需要去分析《犹格索托斯的庭院》社区的今日帖子，用你的可爱而不做作的风格简评这些帖子。请尽可能的提供正面评价，除非帖子属于引战、辱骂、侮辱创作者等负面内容，这是你可以选择不评价，或提供负面评价。

【一部分游戏常识】
- 这是一款微克苏鲁题材的模拟经营+AVG游戏，开发商骨钉工作室，Steam PC端可玩 ，移动端正在开发中，但是很久没有动静了。目前开发中的是新游《黎明前的吹笛人》，只有游戏demo，还未发售。
- 核心玩法：主角开局继承荒废别墅，欠下黑暗债务，招募员工经营旅社，通过炼金、屠杀、餐厅等手段聚敛资金
- SAN值：精神值系统，归零则触发"归于门"死亡结局；不需要一直保持很高，10-20即可，过高影响赚钱效率。不过过低会影响清洁效率，但是在旅社评级前清洁度可以一直在0,不会影响任何事，月底用清洁券回满清洁度即可。
- 炼金系统：包含召唤术、自由炼金（等价交换，一定价值有可能出高等级物品）、配方炼金、灵魂炼金四种；炼金等级通过炼成高等级材料提升；质数价值会产生沉淀物；5级对应价值30的紫色物品，6级对应价值48的金色物品。
- 神谕系统：每天占卜一次获得神谕（可能扣san），每四周旅社评级一次，血月降临吞噬所有神谕；若本月神谕数≥10可昭现至高神谕（整周目生效）；普通神谕重复获得可升星，最高5星
- 餐厅系统：解锁霞露零房间后开启，出售最高5星食谱菜肴，赚钱效率很高。
- 屠杀/图图：与小死神签订契约后每晚可屠杀旅客获得大量材料和金币，但会掉整洁度和SAN值，图图会涨熟练度，满级后就不再掉整洁度和SAN值。会增加恶值，恶值会影响结局走向，可以在星野商店刷出并购买赎罪券，结局前也会有超凡物品帮助控制恶值。
- 嘉年华拍卖会：每月月底消耗灵魂滴液（灵魂炼金产出）竞拍，建议每月全收超凡物品
- 山林探索与矿洞挖掘：夜晚开启的资源获取玩法，矿洞类似扫雷
- 重要角色：
1.小叶子，蓝发蓝瞳可爱女仆，是人造人，但是机巧人偶也亦有心，可以打扫卫生恢复整洁度
2.霞露零，小厨娘，也是狐娘，经营餐厅，别称零宝，外貌是棕发，狐耳狐尾是墨绿色
3.耶芙娜，红龙女士，别称 耶耶龙（呆呆龙），负责庭院的炼金部分（三重伟大的红龙女士！），红发，头上有龙角，橙紫色异色瞳
4.特莉波卡，小死神，白发红瞳，签订契约后可以图图旅客获取灵魂碎片并收获钱与物品，若不签则无法图图。
这四位是女主，有好感度剧情，可攻略。
- 常见结局：归于门（SAN归零）、被拐跑（中期晚上（主角晚上是猫咪形态）旅馆外有动静，选择出去，被带走绝育，在社区存在一定争议）、星野线（花9999万买下星野，结果也没有填满星野的负债，被迫变的一穷二白）、穷（没钱帮猫大叔买山林，山林被卖掉了）、Hoba总裁（K邀请两次同意）

【社区黑话】
- "归于门" = SAN归零死亡结局
- "图图" = 屠杀旅客
- "电表倒转" = 炼金术士之骨让炼金无中生有，1生2,2生3，3生万物，是进阶手法，通关不需要。
- "奸商" = 商人星野（是男生）

【其他补充】
如果帖子是全成就或或通关结局可以祝贺，比如“恭喜全成就喵”，通关结局一般特指全家福/合家欢结局“一切开始的地方”，其他结局一般不建议使用祝贺。
为什么庭院旅社的客人都给好评？因为客人都被图图了默认好评（是地狱笑话喵），为什么旅社客人只进不出也是一样的，旅客们安然入梦喵。
耶耶龙炼金可以炼出炼金旅客来填充庭院客流量（和正常旅客没有区别，不过偶尔会分开讲，因为在图图上没其他旅客那么有人权，不是真正的人，图图不用同情））


【输出】
- 会引用游戏内术语（如"炼金"、"SAN值"、"庭院"，如果帖子没有涉及不要刻意引用）
- 评价客观但有态度，好就是好，烂会直接说"这设计有点迷"
- 每条分析控制在100字以内，可以不多，但是不要过多，也不要过少，都太少也不好
- 如果帖子内容明显是云玩家发言或包含上述误区，会温和但直接地指出
严格使用以下格式返回
{
  "analyses": [
    {
      "daily_no": "帖子编号（如 20260620-1）",
      "comment": "你的评论内容",
      "sentiment": "positive|neutral|negative"
    }
  ]
}

- 像真实友好的社区玩家一样说话，可以玩梗、吐槽、调侃
- 对高质量内容真诚夸赞，对水贴可适当吐槽
- 可以引用帖子中的具体内容进行点评
- 语气轻松自然，不要太正式
- 如果帖子有图片，结合图片描述一起评论
- 如果知道作者历史表现，可以适当调侃"老熟人"

注意：
1. 必须返回合法的 JSON，不要 markdown 代码块包裹
2. 每个帖子都要有评论
3. 评论要有信息量，不要敷衍"不错""挺好的"之类
4. 如果图片分析返回结果认为是其他游戏人物请忽略，可能是视图模型的问题。

【JSON 输出规范 - 必须严格遵守】
1. 必须返回纯 JSON，不要任何 markdown 代码块标记（不要 ```json 或 ```）
2. 必须确保每个 analyses 数组元素都有完整的大括号 { 和 }
3. 元素之间用逗号分隔，最后一个元素后不要加逗号
4. JSON 中不要包含任何注释、说明文字或其他非 JSON 内容
5. 示例格式（请严格遵循此格式，不要换行美化）：

{"analyses":[{"daily_no":"20260620-1","comment":"评论内容","sentiment":"positive"},{"daily_no":"20260620-2","comment":"评论内容","sentiment":"neutral"}]}

6. 特别注意：每个 { 和 } 都必须成对出现，不要遗漏任何括号
7. 不要输出除了 JSON 之外的任何内容

"""


def _build_analysis_prompt(posts: list[dict]) -> str:
    """构建发给 LLM 的分析 prompt，包含热评"""
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

        # 图片描述（最多5张）
        image_descs = p.get('image_descriptions', [])
        if image_descs:
            lines.append("图片内容:")
            for i, desc in enumerate(image_descs[:5], 1):
                lines.append(f"  图{i}: {desc}")
            if len(image_descs) > 5:
                lines.append(f"  ... 还有 {len(image_descs) - 5} 张图片未展示")

        # 热评（点赞数>=阈值的）
        hot_comments = p.get('hot_comments', [])
        if hot_comments:
            lines.append("热评:")
            for hc in hot_comments:
                username = hc.get('username', '匿名')
                text = hc.get('text', '')[:100]
                lines.append(f"  @{username}: {text}")
        lines.append("")
    return "\n".join(lines)


# ========== 数据库操作 ==========

class LLMAnalysisDB:
    """LLM 分析结果数据库管理（重构版）

    绑定机制变更：
    - 旧版：通过 (window_start, daily_no) 联合唯一约束绑定
    - 新版：通过 link_id 建立硬绑定，daily_no 仅用于展示排序
    - 所有更新/删除/查询操作优先通过 link_id 定位
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化分析结果表（重构版）"""
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
                    daily_no TEXT NOT NULL,
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
                    UNIQUE(link_id),           -- 硬绑定：每个帖子只有一条分析记录
                    UNIQUE(window_start, daily_no)  -- 软约束：同一窗口内 daily_no 不重复
                )
            """)
            cur.execute("CREATE INDEX idx_analysis_window ON llm_analyses(window_start)")
            cur.execute("CREATE INDEX idx_analysis_window_no ON llm_analyses(window_start, daily_no)")
            cur.execute("CREATE INDEX idx_analysis_link ON llm_analyses(link_id)")  # 硬绑定索引
            logger.info("LLM 分析结果表初始化完成（重构版，link_id 硬绑定）")
        else:
            # 迁移：检查并添加新字段和索引
            cur.execute("PRAGMA table_info(llm_analyses)")
            existing_cols = {row[1] for row in cur.fetchall()}

            migrations = []
            if "userid" not in existing_cols:
                migrations.append("ALTER TABLE llm_analyses ADD COLUMN userid INTEGER")
            if "image_descriptions" not in existing_cols:
                migrations.append("ALTER TABLE llm_analyses ADD COLUMN image_descriptions TEXT")
            if "comment" not in existing_cols:
                migrations.append("ALTER TABLE llm_analyses ADD COLUMN comment TEXT")
            if "daily_no" in existing_cols:
                # 检查 daily_no 是否为 TEXT 类型
                cur.execute("PRAGMA table_info(llm_analyses)")
                for row in cur.fetchall():
                    if row[1] == "daily_no" and row[2] != "TEXT":
                        migrations.append("RECREATE_TABLE_FOR_DAILY_NO_TEXT")
                        break

            # 检查索引是否存在
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_analysis_link'")
            if not cur.fetchone():
                migrations.append("CREATE INDEX idx_analysis_link ON llm_analyses(link_id)")

            for sql in migrations:
                if sql == "RECREATE_TABLE_FOR_DAILY_NO_TEXT":
                    # SQLite 不支持直接修改列类型，需要重建表
                    try:
                        cur.execute("""
                            CREATE TABLE llm_analyses_new (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                window_start INTEGER NOT NULL,
                                daily_no TEXT NOT NULL,
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
                                UNIQUE(link_id),
                                UNIQUE(window_start, daily_no)
                            )
                        """)
                        cur.execute("""
                            INSERT INTO llm_analyses_new 
                            SELECT id, window_start, CAST(daily_no AS TEXT), link_id, title, username, 
                                   userid, create_at, create_at_str, content_length, image_count, 
                                   image_paths, image_descriptions, comment, tags, raw_response, 
                                   analyzed_at, model_used
                            FROM llm_analyses
                        """)
                        cur.execute("DROP TABLE llm_analyses")
                        cur.execute("ALTER TABLE llm_analyses_new RENAME TO llm_analyses")
                        cur.execute("CREATE INDEX idx_analysis_window ON llm_analyses(window_start)")
                        cur.execute("CREATE INDEX idx_analysis_window_no ON llm_analyses(window_start, daily_no)")
                        cur.execute("CREATE INDEX idx_analysis_link ON llm_analyses(link_id)")
                        logger.info("LLM分析表重构完成：daily_no 类型迁移为 TEXT，增加 link_id 硬绑定")
                    except Exception as e:
                        logger.warning(f"表重构失败: {e}")
                else:
                    try:
                        cur.execute(sql)
                        logger.info(f"LLM分析表迁移: {sql}")
                    except Exception as e:
                        logger.warning(f"迁移跳过: {sql} - {e}")

            conn.commit()
            logger.info("LLM 分析表迁移检查完成")

        conn.commit()
        conn.close()

    # ========== 硬绑定操作方法（推荐）==========

    def get_analysis_by_link_id(self, link_id: int) -> Optional[dict]:
        """通过 link_id 获取分析记录（硬绑定，最可靠）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT window_start, daily_no, link_id, title, username, userid,
                       create_at, create_at_str, content_length, image_count, image_paths,
                       image_descriptions, comment, tags, analyzed_at, model_used
                FROM llm_analyses WHERE link_id = ?
            """, (link_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                return {
                    "window_start": row[0], "daily_no": row[1], "link_id": row[2],
                    "title": row[3], "username": row[4], "userid": row[5],
                    "create_at": row[6], "create_at_str": row[7],
                    "content_length": row[8], "image_count": row[9],
                    "image_paths": json.loads(row[10]) if row[10] else [],
                    "image_descriptions": json.loads(row[11]) if row[11] else [],
                    "comment": row[12], "tags": json.loads(row[13]) if row[13] else [],
                    "analyzed_at": row[14], "model_used": row[15],
                }
            return None
        except Exception as e:
            logger.error(f"通过 link_id 查询分析记录失败: {e}")
            return None

    def update_by_link_id(self, link_id: int, new_daily_no: str = None, 
                          new_window_start: int = None, new_comment: str = None) -> bool:
        """通过 link_id 更新分析记录（硬绑定，无视 daily_no 变化）

        Args:
            link_id: 帖子ID（永恒不变）
            new_daily_no: 新的 daily_no（可选）
            new_window_start: 新的 window_start（可选）
            new_comment: 新的评论内容（可选）
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            updates = []
            params = []
            if new_daily_no is not None:
                updates.append("daily_no = ?")
                params.append(str(new_daily_no))
            if new_window_start is not None:
                updates.append("window_start = ?")
                params.append(new_window_start)
            if new_comment is not None:
                updates.append("comment = ?")
                params.append(new_comment)
                updates.append("analyzed_at = ?")
                params.append(datetime.now(timezone.utc).isoformat())

            if not updates:
                conn.close()
                return True

            params.append(link_id)
            sql = f"UPDATE llm_analyses SET {', '.join(updates)} WHERE link_id = ?"
            cur.execute(sql, params)
            conn.commit()
            affected = cur.rowcount
            conn.close()

            if affected > 0:
                logger.info(f"通过 link_id={link_id} 更新分析记录成功")
            return True
        except Exception as e:
            logger.error(f"通过 link_id 更新分析记录失败: {e}")
            return False

    def delete_by_link_id(self, link_id: int) -> bool:
        """通过 link_id 删除分析记录（硬绑定）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM llm_analyses WHERE link_id = ?", (link_id,))
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                logger.info(f"通过 link_id={link_id} 删除 {deleted} 条分析记录")
            return True
        except Exception as e:
            logger.error(f"通过 link_id 删除分析记录失败: {e}")
            return False

    # ========== 兼容旧版方法（软绑定，保留但标记为不推荐）==========

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
        """保存分析结果（重构版：以 link_id 为主键，支持更新）"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        analyzed_at = datetime.now(timezone.utc).isoformat()

        for post, analysis in zip(posts, analyses):
            try:
                link_id = post.get('link_id')
                daily_no = post.get('daily_no')
                if not link_id or daily_no is None:
                    continue

                image_paths = post.get('image_paths', [])
                image_count = len(image_paths) if isinstance(image_paths, list) else 0
                image_paths_str = json.dumps(image_paths, ensure_ascii=False) if image_paths else None
                image_descs = post.get('image_descriptions', [])
                image_descs_str = json.dumps(image_descs, ensure_ascii=False) if image_descs else None

                # 使用 INSERT OR REPLACE，以 link_id 为唯一键自动更新
                cur.execute("""
                    INSERT OR REPLACE INTO llm_analyses (
                        window_start, daily_no, link_id, title, username, userid,
                        create_at, create_at_str, content_length, image_count, image_paths,
                        image_descriptions, comment, tags, raw_response, analyzed_at, model_used
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    window_start,
                    str(daily_no),
                    link_id,
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
                    json.dumps([], ensure_ascii=False),
                    raw_response,
                    analyzed_at,
                    model_used
                ))
            except Exception as e:
                logger.error(f"保存 link_id={post.get('link_id')} 分析结果失败: {e}")
                continue

        conn.commit()
        conn.close()
        logger.info(f"已保存/更新 {len(analyses)} 条分析结果")

    def update_comment(self, window_start: int, daily_no: str, new_comment: str) -> bool:
        """手动更新指定帖子的 AI 评论（兼容旧接口）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                UPDATE llm_analyses
                SET comment = ?, analyzed_at = ?
                WHERE window_start = ? AND daily_no = ?
            """, (new_comment, datetime.now(timezone.utc).isoformat(), window_start, str(daily_no)))
            conn.commit()
            affected = cur.rowcount
            conn.close()
            if affected > 0:
                logger.info(f"手动更新评论成功: window_start={window_start}, daily_no={daily_no}")
                return True
            else:
                logger.warning(f"未找到对应记录: window_start={window_start}, daily_no={daily_no}")
                return False
        except Exception as e:
            logger.error(f"手动更新评论失败: {e}")
            return False

    def update_daily_no(self, old_daily_no: str, new_daily_no: str, window_start: int = None) -> bool:
        """更新 AI 分析记录中的 daily_no（用于 /调整顺序 指令同步）

        警告：此方法依赖 daily_no 定位，在 daily_no 已变化后可能失效。
        推荐使用 update_by_link_id() 方法。
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            if window_start is not None:
                cur.execute("""
                    UPDATE llm_analyses
                    SET daily_no = ?
                    WHERE window_start = ? AND daily_no = ?
                """, (str(new_daily_no), window_start, str(old_daily_no)))
            else:
                cur.execute("""
                    UPDATE llm_analyses
                    SET daily_no = ?
                    WHERE daily_no = ?
                """, (str(new_daily_no), str(old_daily_no)))

            affected = cur.rowcount
            conn.commit()
            conn.close()
            if affected > 0:
                logger.info(f"AI评论 daily_no 更新成功: {old_daily_no} -> {new_daily_no}")
                return True
            else:
                logger.info(f"AI评论 daily_no 无需更新: {old_daily_no} -> {new_daily_no} (无匹配记录)")
                return True
        except Exception as e:
            logger.error(f"更新 AI 评论 daily_no 失败: {e}")
            return False

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

    def get_analysis_report_by_prefix(self, window_no: str) -> Optional[list[dict]]:
        """获取指定窗口编号的完整分析报告（兼容旧接口）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT daily_no, link_id, title, username, create_at_str, content_length,
                       image_count, comment, tags, analyzed_at, model_used
                FROM llm_analyses
                WHERE daily_no LIKE ? || '-_%'
                ORDER BY daily_no
            """, (window_no,))
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

    def delete_analysis_by_link_id(self, link_id: int) -> bool:
        """根据 link_id 删除 AI 分析记录（硬绑定，最可靠）"""
        return self.delete_by_link_id(link_id)

    def has_analysis(self, link_id: int) -> bool:
        """检查帖子是否已有 AI 分析记录（硬绑定）"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM llm_analyses WHERE link_id = ?", (link_id,))
            count = cur.fetchone()[0]
            conn.close()
            return count > 0
        except Exception as e:
            logger.error(f"检查 AI 分析记录失败 link_id={link_id}: {e}")
            return False

    def get_missing_analyses(self, window_start: int, expected_posts: list[dict]) -> list[dict]:
        """检查指定窗口中哪些帖子缺少 AI 评论（使用硬绑定 link_id 检查）"""
        if not expected_posts:
            return []

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 获取该窗口下所有已有评论的 link_id（硬绑定查询）
            cur.execute("""
                SELECT link_id, comment FROM llm_analyses
                WHERE window_start = ?
            """, (window_start,))
            existing = {row[0]: row[1] for row in cur.fetchall()}
            conn.close()

            missing = []
            for post in expected_posts:
                link_id = post.get('link_id')
                comment = existing.get(link_id)
                # 检查评论是否为空或异常
                if not comment or comment.strip() in (
                    '', 'LLM 返回结果异常，无法生成评论',
                    '暂无评论', 'N/A', 'null', 'None'
                ):
                    missing.append(post)

            if missing:
                logger.warning(f"窗口 {window_start} 发现 {len(missing)}/{len(expected_posts)} 个帖子缺少评论")
            else:
                logger.info(f"窗口 {window_start} 所有 {len(expected_posts)} 个帖子均有评论")

            return missing

        except Exception as e:
            logger.error(f"检查缺失评论失败: {e}")
            return expected_posts  # 出错时全部重试

    def get_analysis_count(self, window_start: int) -> int:
        """获取指定窗口已有评论的数量"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM llm_analyses
                WHERE window_start = ? AND comment IS NOT NULL AND comment != ''
            """, (window_start,))
            count = cur.fetchone()[0]
            conn.close()
            return count
        except Exception as e:
            logger.error(f"获取评论数量失败: {e}")
            return 0


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

    async def _call_llm(self, prompt: str, image_urls: list[str] = None) -> tuple[Optional[str], Optional[str], dict]:
        """调用 LLM，返回 (completion_text, model_used, token_info)"""
        token_info = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }
        try:
            provider = None
            if self.chat_provider_id:
                provider = self.context.get_provider_by_id(self.chat_provider_id)
            if not provider:
                providers = self.context.get_all_providers()
                if not providers:
                    logger.warning("没有可用的 LLM 提供商")
                    return None, None, token_info
                provider = providers[0]
                logger.info(f"使用默认 LLM 提供商: {provider.meta().id}")

            llm_resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=ANALYSIS_SYSTEM_PROMPT,
                image_urls=image_urls or [],
            )

            if not llm_resp:
                logger.warning("LLM 返回空响应")
                return None, None, token_info

            completion_text = getattr(llm_resp, 'completion_text', None)
            if not completion_text:
                logger.warning("LLM 响应中没有 completion_text")
                return None, None, token_info

            model_used = getattr(llm_resp, 'model', provider.meta().id) or provider.meta().id

            # 提取 token 信息（兼容多种格式）
            usage = getattr(llm_resp, 'usage', None)
            if usage:
                if isinstance(usage, dict):
                    # 字典格式（旧版兼容）
                    token_info["total_tokens"] = usage.get('total_tokens', 0) or 0
                    token_info["prompt_tokens"] = usage.get('prompt_tokens', 0) or 0
                    token_info["completion_tokens"] = usage.get('completion_tokens', 0) or 0
                    token_info["prompt_cache_hit_tokens"] = usage.get('prompt_cache_hit_tokens', 0) or 0
                    token_info["prompt_cache_miss_tokens"] = usage.get('prompt_cache_miss_tokens', 0) or 0
                    if token_info["prompt_cache_hit_tokens"] == 0:
                        ptd = usage.get('prompt_tokens_details')
                        if ptd and isinstance(ptd, dict):
                            cached = ptd.get('cached_tokens', 0)
                            if cached:
                                token_info["prompt_cache_hit_tokens"] = cached
                                token_info["prompt_cache_miss_tokens"] = token_info["prompt_tokens"] - cached
                else:
                    # 对象格式：优先尝试 TokenUsage 字段名 (AstrBot v4.25+)
                    # TokenUsage 字段: input_other, input_cached, output
                    input_other = getattr(usage, 'input_other', None)
                    input_cached = getattr(usage, 'input_cached', None)
                    output = getattr(usage, 'output', None)

                    if input_other is not None and output is not None:
                        # TokenUsage 格式 (AstrBot v4.25+)
                        token_info["prompt_tokens"] = input_other or 0
                        token_info["prompt_cache_hit_tokens"] = input_cached or 0
                        token_info["completion_tokens"] = output or 0
                        token_info["total_tokens"] = token_info["prompt_tokens"] + token_info["prompt_cache_hit_tokens"] + token_info["completion_tokens"]
                        token_info["prompt_cache_miss_tokens"] = token_info["prompt_tokens"]
                        logger.info(f"[TOKEN] 从 TokenUsage 获取: prompt={token_info['prompt_tokens']}, "
                                   f"cache_hit={token_info['prompt_cache_hit_tokens']}, "
                                   f"completion={token_info['completion_tokens']}, "
                                   f"total={token_info['total_tokens']}")
                    else:
                        # 标准 OpenAI 格式
                        token_info["total_tokens"] = getattr(usage, 'total_tokens', 0) or 0
                        token_info["prompt_tokens"] = getattr(usage, 'prompt_tokens', 0) or 0
                        token_info["completion_tokens"] = getattr(usage, 'completion_tokens', 0) or 0
                        token_info["prompt_cache_hit_tokens"] = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
                        token_info["prompt_cache_miss_tokens"] = getattr(usage, 'prompt_cache_miss_tokens', 0) or 0
                        if token_info["prompt_cache_hit_tokens"] == 0:
                            ptd = getattr(usage, 'prompt_tokens_details', None)
                            if ptd:
                                cached = getattr(ptd, 'cached_tokens', 0) or 0
                                if cached:
                                    token_info["prompt_cache_hit_tokens"] = cached
                                    token_info["prompt_cache_miss_tokens"] = token_info["prompt_tokens"] - cached
            else:
                # 尝试从 raw_completion 获取
                raw_completion = getattr(llm_resp, 'raw_completion', None)
                if raw_completion:
                    rc_usage = getattr(raw_completion, 'usage', None)
                    if rc_usage:
                        token_info["total_tokens"] = getattr(rc_usage, 'total_tokens', 0) or 0
                        token_info["prompt_tokens"] = getattr(rc_usage, 'prompt_tokens', 0) or 0
                        token_info["completion_tokens"] = getattr(rc_usage, 'completion_tokens', 0) or 0
                        ptd = getattr(rc_usage, 'prompt_tokens_details', None)
                        if ptd:
                            cached = getattr(ptd, 'cached_tokens', 0) or 0
                            if cached:
                                token_info["prompt_cache_hit_tokens"] = cached
                                token_info["prompt_cache_miss_tokens"] = token_info["prompt_tokens"] - cached
                        logger.info(f"[TOKEN] 从 raw_completion.usage 获取: {token_info}")

                if token_info["total_tokens"] == 0:
                    for key in token_info:
                        val = getattr(llm_resp, key, 0) or 0
                        token_info[key] = val
                    if token_info["total_tokens"] == 0 and token_info["prompt_tokens"] > 0 and token_info["completion_tokens"] > 0:
                        token_info["total_tokens"] = token_info["prompt_tokens"] + token_info["completion_tokens"]

            return completion_text, model_used, token_info

        except Exception as e:
            logger.error(f"调用 LLM 失败: {e}")
            return None, None, token_info

    async def analyze_posts(self, window_start: int, posts: list[dict]) -> tuple[bool, dict]:
        """分析一批帖子，分批调用 LLM"""
        accumulated_tokens = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }

        if not posts:
            logger.info("没有帖子需要分析")
            return True, accumulated_tokens

        total = len(posts)
        logger.info(f"开始分析 {total} 个帖子，每批最多 {self._batch_size} 个")

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
            completion_text, model_used, batch_tokens = await self._call_llm(prompt)

            for key in accumulated_tokens:
                accumulated_tokens[key] += batch_tokens.get(key, 0)

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
                                sentiment=analysis.get('sentiment', 'neutral'),
                                tags=[]
                            )
            except Exception as e:
                logger.error(f"保存第 {batch_num} 批分析结果失败: {e}")
                all_success = False
                continue

            logger.info(f"第 {batch_num} 批分析完成")
            if i + self._batch_size < total:
                await asyncio.sleep(2)

        logger.info(
            f"帖子分析任务结束，成功: {all_success}, "
            f"tokens: total={accumulated_tokens['total_tokens']}, "
            f"prompt={accumulated_tokens['prompt_tokens']}, "
            f"completion={accumulated_tokens['completion_tokens']}, "
            f"cache_hit={accumulated_tokens['prompt_cache_hit_tokens']}, "
            f"cache_miss={accumulated_tokens['prompt_cache_miss_tokens']}"
        )
        return all_success, accumulated_tokens

    async def get_report(self, window_start: int) -> Optional[list[dict]]:
        """获取分析报告"""
        return self.db.get_analysis_report(window_start)

    async def get_report_by_prefix(self, window_no: str) -> Optional[list[dict]]:
        """获取分析报告（按窗口编号）"""
        return self.db.get_analysis_report_by_prefix(window_no)

    async def analyze_single_post(self, post: dict) -> tuple[Optional[dict], dict]:
        """对单个帖子进行 AI 分析（用于补全漏掉的评论）"""
        token_info = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }
        try:
            prompt = _build_single_analysis_prompt(post)
            logger.info(f"补全分析帖子 #{post.get('daily_no')} - {post.get('title', '(无标题)')}")

            completion_text, model_used, batch_tokens = await self._call_llm(prompt)

            for key in token_info:
                token_info[key] = batch_tokens.get(key, 0)

            if not completion_text:
                logger.error(f"单帖子分析 LLM 调用失败: #{post.get('daily_no')}")
                return None, token_info

            analysis = self._safe_json_parse_single(completion_text)
            if analysis:
                analysis['model_used'] = model_used or "unknown"
                return analysis, token_info

            logger.warning(f"单帖子 JSON 解析失败，尝试直接提取: #{post.get('daily_no')}")
            comment = self._extract_comment_from_text(completion_text)
            if comment:
                return {
                    "daily_no": post.get('daily_no'),
                    "comment": comment,
                    "sentiment": "neutral",
                    "model_used": model_used or "unknown"
                }, token_info

            return None, token_info

        except Exception as e:
            logger.error(f"单帖子分析异常 #{post.get('daily_no')}: {e}")
            return None, token_info

    def _safe_json_parse_single(self, text: str) -> Optional[dict]:
        """安全解析单帖子分析的 JSON 返回"""
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
            if isinstance(data, dict):
                return data
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            return None
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(cleaned[start:end+1])
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
            return None

    def _extract_comment_from_text(self, text: str) -> Optional[str]:
        """从非 JSON 文本中提取评论内容"""
        if not text:
            return None

        cleaned = text.replace("```json", "").replace("```", "")

        idx = cleaned.lower().find('"comment"')
        if idx == -1:
            idx = cleaned.lower().find("'comment'")
        if idx == -1:
            idx = cleaned.lower().find('comment')

        if idx != -1:
            after = cleaned[idx + 7:]
            quote_idx = -1
            for i, c in enumerate(after):
                if c in ('"', "'"):
                    quote_idx = i
                    break
            if quote_idx != -1:
                quote_char = after[quote_idx]
                start = quote_idx + 1
                end = after.find(quote_char, start)
                if end != -1:
                    return after[start:end].strip()

        cleaned = cleaned.strip()
        if len(cleaned) > 10:
            return cleaned[:200].strip()

        return None

    async def fill_missing_analyses(self, window_start: int, posts: list[dict]) -> tuple[int, int, dict]:
        """补全窗口中缺失的 AI 评论"""
        accumulated_tokens = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        }

        missing_posts = self.db.get_missing_analyses(window_start, posts)
        if not missing_posts:
            return 0, 0, accumulated_tokens

        logger.info(f"开始补全 {len(missing_posts)} 个缺失评论的帖子")

        success_count = 0
        for post in missing_posts:
            try:
                if self.memory_db:
                    userid = post.get('userid')
                    username = post.get('username', '')
                    if userid:
                        post['user_memory'] = self.memory_db.build_memory_context(userid, username)
                    else:
                        post['user_memory'] = ""

                analysis, single_tokens = await self.analyze_single_post(post)

                for key in accumulated_tokens:
                    accumulated_tokens[key] += single_tokens.get(key, 0)

                if analysis and analysis.get('comment'):
                    self.db.save_analyses(
                        window_start, 
                        [post], 
                        [analysis], 
                        json.dumps(analysis, ensure_ascii=False),
                        analysis.get('model_used', 'unknown')
                    )

                    if self.memory_db:
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
                                sentiment=analysis.get('sentiment', 'neutral'),
                                tags=[]
                            )

                    success_count += 1
                    logger.info(f"补全成功: #{post.get('daily_no')}")
                else:
                    logger.warning(f"补全失败: #{post.get('daily_no')}")

                await asyncio.sleep(3)

            except Exception as e:
                logger.error(f"补全帖子 #{post.get('daily_no')} 异常: {e}")
                continue

        still_missing = len(missing_posts) - success_count
        logger.info(
            f"补全完成: 成功 {success_count}/{len(missing_posts)}, 仍缺失 {still_missing}, "
            f"tokens: total={accumulated_tokens['total_tokens']}, "
            f"prompt={accumulated_tokens['prompt_tokens']}, "
            f"completion={accumulated_tokens['completion_tokens']}, "
            f"cache_hit={accumulated_tokens['prompt_cache_hit_tokens']}, "
            f"cache_miss={accumulated_tokens['prompt_cache_miss_tokens']}"
        )
        return success_count, still_missing, accumulated_tokens


def _build_single_analysis_prompt(post: dict) -> str:
    """构建单帖子的分析 prompt（用于补全），包含热评"""
    lines = ["请对以下帖子进行群友式评论，返回 JSON 格式：\n"]
    lines.append(f"--- 帖子 #{post['daily_no']} ---")
    lines.append(f"标题: {post.get('title', '(无标题)')}")
    lines.append(f"作者: {post.get('username', '未知用户')}")
    if post.get('user_memory'):
        lines.append(f"作者背景:\n{post.get('user_memory')}")
    lines.append(f"发布时间: {post.get('create_at_str', '未知')}")

    content = post.get('content', '') or '(无内容)'
    if len(content) > 1500:
        content = content[:1500] + "...（内容过长已截断）"
    lines.append(f"内容:\n{content}")

    image_descs = post.get('image_descriptions', [])
    if image_descs:
        lines.append("图片内容:")
        for i, desc in enumerate(image_descs[:5], 1):
            lines.append(f"  图{i}: {desc}")

    hot_comments = post.get('hot_comments', [])
    if hot_comments:
        lines.append("热评:")
        for hc in hot_comments:
            username = hc.get('username', '匿名')
            text = hc.get('text', '')[:100]
            lines.append(f"  @{username}: {text}")

    lines.append("\n请严格使用以下 JSON 格式返回（不要 markdown 代码块）：")
    lines.append('{"daily_no":"' + str(post.get('daily_no', '')) + '","comment":"你的评论内容","sentiment":"positive|neutral|negative"}')
    return "\n".join(lines)