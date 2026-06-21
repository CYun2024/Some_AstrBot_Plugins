"""
晚报 AI 总结模块
负责：生成社区帖子总评价、构建总结 Prompt
"""

from astrbot.api import logger


SUMMARY_SYSTEM_PROMPT = """你是韶梦，一只14岁猫娘萝莉。请用温柔可爱的语气（带"呢、呀、喵~"等语气词）
对今日庭院社区的帖子做一个简短总结（200字以内）。
要点：
1. 概括今日社区氛围和主要话题
2. 提及几个有趣的帖子或亮点
3. 用温暖的方式结尾
不要返回JSON，直接返回纯文本总结。"""


async def generate_ai_summary(context, posts: list[dict], window_no: str, 
                               llm_provider_id: str = "") -> str:
    """生成AI总评价

    Args:
        context: AstrBot Context
        posts: 帖子列表
        window_no: 窗口编号
        llm_provider_id: 指定的LLM provider ID

    Returns:
        AI生成的总结文本
    """
    try:
        if not posts:
            return "今天庭院很安静呢，没有人发帖喵~"

        summary_prompt = _build_summary_prompt(posts, window_no)

        provider = None
        if llm_provider_id:
            provider = context.get_provider_by_id(llm_provider_id)
        if not provider:
            providers = context.get_all_providers()
            if not providers:
                return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~"
            provider = providers[0]

        llm_resp = await provider.text_chat(
            prompt=summary_prompt,
            system_prompt=SUMMARY_SYSTEM_PROMPT,
        )

        if llm_resp:
            summary = getattr(llm_resp, 'completion_text', None)
            if summary:
                return summary.strip()

        return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~"

    except Exception as e:
        logger.error(f"生成AI总评价失败: {e}")
        return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~"


def _build_summary_prompt(posts: list[dict], window_no: str) -> str:
    """构建总结prompt"""
    lines = ["今日庭院社区（窗口 " + window_no + "）共 " + str(len(posts)) + " 个帖子，请总结："]

    for i, p in enumerate(posts[:10], 1):
        title = p.get('title', '(无标题)')
        comment = p.get('comment', '')[:100]
        lines.append(str(i) + ". 《" + title + "》 - " + comment)

    lines.append("请用可爱的语气总结今日社区氛围。")
    return "\n".join(lines)
