"""
晚报 AI 总结模块
负责：生成社区帖子总评价、构建总结 Prompt
"""

import json
from astrbot.api import logger


SUMMARY_SYSTEM_PROMPT = """你是韶梦，一只14岁猫娘萝莉。请用温柔可爱的语气（带"呢、呀、喵~"等语气词）
对今日庭院社区的帖子做一个简短总结（50-100字左右，不要超过100字）。
要点：
1. 概括今日社区氛围和主要话题
2. 提及1-2个有趣的帖子或亮点
3. 用温暖的方式结尾
不要返回JSON，直接返回纯文本总结。"""


async def generate_ai_summary(context, posts: list[dict], window_no: str,
                               llm_provider_id: str = "") -> tuple[str, str, dict]:
    """生成AI总评价，返回 (总结文本, 模型名, token信息)"""
    token_info = {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }
    try:
        if not posts:
            logger.info("[AI总评] posts为空，返回默认文案")
            return "今天庭院很安静呢，没有人发帖喵~", "", token_info

        summary_prompt = _build_summary_prompt(posts, window_no)
        logger.info(f"[AI总评] 开始生成，窗口={window_no}，帖子数={len(posts)}")

        provider = None
        if llm_provider_id:
            provider = context.get_provider_by_id(llm_provider_id)
        if not provider:
            providers = context.get_all_providers()
            if not providers:
                logger.warning("[AI总评] 没有可用的LLM提供商")
                return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", "", token_info
            provider = providers[0]

        llm_resp = await provider.text_chat(
            prompt=summary_prompt,
            system_prompt=SUMMARY_SYSTEM_PROMPT,
        )

        model_used = getattr(llm_resp, 'model', None) or provider.meta().id

        # ====== 修复：提取 token 信息（兼容对象和字典 + TokenUsage） ======
        usage = getattr(llm_resp, 'usage', None)
        if usage:
            if isinstance(usage, dict):
                # 字典格式（旧版兼容）
                token_info["total_tokens"] = usage.get('total_tokens', 0) or 0
                token_info["prompt_tokens"] = usage.get('prompt_tokens', 0) or 0
                token_info["completion_tokens"] = usage.get('completion_tokens', 0) or 0
                token_info["prompt_cache_hit_tokens"] = usage.get('prompt_cache_hit_tokens', 0) or 0
                token_info["prompt_cache_miss_tokens"] = usage.get('prompt_cache_miss_tokens', 0) or 0
                logger.debug(f"[AI总评] Token 从字典提取: {token_info}")
            else:
                # 对象格式：优先尝试 TokenUsage 字段名 (AstrBot v4.25+)
                input_other = getattr(usage, 'input_other', None)
                input_cached = getattr(usage, 'input_cached', None)
                output = getattr(usage, 'output', None)

                if input_other is not None and output is not None:
                    # TokenUsage 格式
                    token_info["prompt_tokens"] = input_other or 0
                    token_info["prompt_cache_hit_tokens"] = input_cached or 0
                    token_info["completion_tokens"] = output or 0
                    token_info["total_tokens"] = token_info["prompt_tokens"] + token_info["prompt_cache_hit_tokens"] + token_info["completion_tokens"]
                    token_info["prompt_cache_miss_tokens"] = token_info["prompt_tokens"]
                    logger.debug(f"[AI总评] Token 从 TokenUsage 提取: {token_info}")
                else:
                    # 标准 OpenAI 格式
                    token_info["total_tokens"] = getattr(usage, 'total_tokens', 0) or 0
                    token_info["prompt_tokens"] = getattr(usage, 'prompt_tokens', 0) or 0
                    token_info["completion_tokens"] = getattr(usage, 'completion_tokens', 0) or 0
                    token_info["prompt_cache_hit_tokens"] = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
                    token_info["prompt_cache_miss_tokens"] = getattr(usage, 'prompt_cache_miss_tokens', 0) or 0
                    logger.debug(f"[AI总评] Token 从对象提取: {token_info}")
        else:
            # 降级处理：尝试 raw_completion
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
                    logger.debug(f"[AI总评] Token 从 raw_completion 提取: {token_info}")
            else:
                raw_usage = getattr(llm_resp, 'raw_usage', None)
                if raw_usage:
                    if isinstance(raw_usage, dict):
                        token_info["total_tokens"] = raw_usage.get('total_tokens', 0) or 0
                        token_info["prompt_tokens"] = raw_usage.get('prompt_tokens', 0) or 0
                        token_info["completion_tokens"] = raw_usage.get('completion_tokens', 0) or 0
                        token_info["prompt_cache_hit_tokens"] = raw_usage.get('prompt_cache_hit_tokens', 0) or 0
                        token_info["prompt_cache_miss_tokens"] = raw_usage.get('prompt_cache_miss_tokens', 0) or 0
                    else:
                        for key in token_info:
                            val = getattr(raw_usage, key, 0) or 0
                            token_info[key] = val
                    logger.debug(f"[AI总评] Token 从 raw_usage 提取: {token_info}")        # 获取总结文本（保持原有逻辑）
        summary = None
        if hasattr(llm_resp, 'completion_text'):
            summary = llm_resp.completion_text
        elif hasattr(llm_resp, 'result'):
            summary = llm_resp.result
        elif hasattr(llm_resp, 'text'):
            summary = llm_resp.text
        elif isinstance(llm_resp, dict):
            summary = llm_resp.get('completion_text') or llm_resp.get('result') or llm_resp.get('text')
        else:
            summary = str(llm_resp)

        if summary and isinstance(summary, str) and summary.strip():
            return summary.strip(), model_used, token_info

        logger.warning(f"[AI总评] 无法获取有效文本")
        return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", model_used, token_info

    except Exception as e:
        logger.error(f"[AI总评] 生成失败: {e}", exc_info=True)
        return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", "", token_info

def _build_summary_prompt(posts: list[dict], window_no: str) -> str:
    """构建总结prompt"""
    lines = ["今日庭院社区（窗口 " + window_no + "）共 " + str(len(posts)) + " 个帖子，请总结："]

    for i, p in enumerate(posts[:10], 1):
        title = p.get('title') or '(无标题)'
        comment = (p.get('comment') or '')[:80]
        lines.append(str(i) + ". 《" + title + "》 - " + comment)

    lines.append("请用可爱的语气总结今日社区氛围（100字以内）。")
    return "\n".join(lines)