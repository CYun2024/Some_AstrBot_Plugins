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
    """生成AI总评价

    Args:
        context: AstrBot Context
        posts: 帖子列表
        window_no: 窗口编号
        llm_provider_id: 指定的LLM provider ID

    Returns:
        (AI生成的总结文本, 使用的模型名, token使用信息字典)
        token字典格式: {
            "total_tokens": int,
            "prompt_tokens": int,
            "completion_tokens": int,
            "prompt_cache_hit_tokens": int,
            "prompt_cache_miss_tokens": int,
        }
    """
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
            logger.info(f"[AI总评] 使用指定provider: {llm_provider_id}")
        if not provider:
            providers = context.get_all_providers()
            if not providers:
                logger.warning("[AI总评] 没有可用的LLM提供商")
                return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", "", token_info
            provider = providers[0]
            logger.info(f"[AI总评] 使用默认provider: {provider}")

        llm_resp = await provider.text_chat(
            prompt=summary_prompt,
            system_prompt=SUMMARY_SYSTEM_PROMPT,
        )

        logger.info(f"[AI总评] LLM响应类型: {type(llm_resp)}")

        model_used = ""

        if llm_resp is None:
            logger.warning("[AI总评] LLM返回None")
            return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", "", token_info

        # 获取模型名
        model_used = getattr(llm_resp, 'model', None)
        if not model_used and provider and hasattr(provider, 'meta'):
            try:
                model_used = provider.meta().id
            except Exception:
                model_used = "unknown"
        if not model_used:
            model_used = "unknown"
        logger.info(f"[AI总评] 模型: {model_used}")

        # ========== 提取 Token 使用信息（增强兼容性）==========
        # 尝试多种方式提取 token，按优先级排序

        # 方式1: 从 raw_usage 字典提取（OpenAI / DeepSeek 标准格式）
        raw_usage = getattr(llm_resp, 'raw_usage', None)
        if raw_usage and isinstance(raw_usage, dict):
            token_info["total_tokens"] = raw_usage.get('total_tokens', 0) or 0
            token_info["prompt_tokens"] = raw_usage.get('prompt_tokens', 0) or 0
            token_info["completion_tokens"] = raw_usage.get('completion_tokens', 0) or 0
            token_info["prompt_cache_hit_tokens"] = raw_usage.get('prompt_cache_hit_tokens', 0) or 0
            token_info["prompt_cache_miss_tokens"] = raw_usage.get('prompt_cache_miss_tokens', 0) or 0
            logger.info(
                f"[AI总评] raw_usage tokens: total={token_info['total_tokens']}, "
                f"prompt={token_info['prompt_tokens']}, completion={token_info['completion_tokens']}, "
                f"cache_hit={token_info['prompt_cache_hit_tokens']}, cache_miss={token_info['prompt_cache_miss_tokens']}"
            )
        else:
            # 方式2: 尝试从 usage 属性提取（AstrBot 某些版本可能用 usage）
            usage = getattr(llm_resp, 'usage', None)
            if usage and isinstance(usage, dict):
                token_info["total_tokens"] = usage.get('total_tokens', 0) or 0
                token_info["prompt_tokens"] = usage.get('prompt_tokens', 0) or 0
                token_info["completion_tokens"] = usage.get('completion_tokens', 0) or 0
                token_info["prompt_cache_hit_tokens"] = usage.get('prompt_cache_hit_tokens', 0) or 0
                token_info["prompt_cache_miss_tokens"] = usage.get('prompt_cache_miss_tokens', 0) or 0
                logger.info(
                    f"[AI总评] usage tokens: total={token_info['total_tokens']}, "
                    f"prompt={token_info['prompt_tokens']}, completion={token_info['completion_tokens']}, "
                    f"cache_hit={token_info['prompt_cache_hit_tokens']}, cache_miss={token_info['prompt_cache_miss_tokens']}"
                )
            else:
                # 方式3: 尝试直接从 llm_resp 属性获取
                token_info["completion_tokens"] = getattr(llm_resp, 'completion_tokens', 0) or 0
                token_info["prompt_tokens"] = getattr(llm_resp, 'prompt_tokens', 0) or 0
                token_info["total_tokens"] = getattr(llm_resp, 'total_tokens', 0) or 0
                # 方式4: 尝试从 response_metadata 获取（LangChain 风格）
                if token_info["total_tokens"] == 0:
                    resp_meta = getattr(llm_resp, 'response_metadata', None)
                    if resp_meta and isinstance(resp_meta, dict):
                        token_usage = resp_meta.get('token_usage', {}) or resp_meta.get('usage', {})
                        if token_usage:
                            token_info["total_tokens"] = token_usage.get('total_tokens', 0) or 0
                            token_info["prompt_tokens"] = token_usage.get('prompt_tokens', 0) or 0
                            token_info["completion_tokens"] = token_usage.get('completion_tokens', 0) or 0
                            token_info["prompt_cache_hit_tokens"] = token_usage.get('prompt_cache_hit_tokens', 0) or 0
                            token_info["prompt_cache_miss_tokens"] = token_usage.get('prompt_cache_miss_tokens', 0) or 0
                            logger.info(
                                f"[AI总评] response_metadata tokens: total={token_info['total_tokens']}, "
                                f"prompt={token_info['prompt_tokens']}, completion={token_info['completion_tokens']}"
                            )

                if token_info["total_tokens"] == 0 and token_info["prompt_tokens"] > 0 and token_info["completion_tokens"] > 0:
                    token_info["total_tokens"] = token_info["prompt_tokens"] + token_info["completion_tokens"]

                logger.info(
                    f"[AI总评] 直接属性 tokens: total={token_info['total_tokens']}, "
                    f"prompt={token_info['prompt_tokens']}, completion={token_info['completion_tokens']}, "
                    f"cache_hit={token_info['prompt_cache_hit_tokens']}, cache_miss={token_info['prompt_cache_miss_tokens']}"
                )

        # 尝试多种方式获取文本内容
        summary = None

        # 方式1: completion_text 属性
        if hasattr(llm_resp, 'completion_text'):
            summary = llm_resp.completion_text
            logger.info(f"[AI总评] 从completion_text获取: {summary[:50] if summary else 'None'}...")

        # 方式2: result 属性
        if not summary and hasattr(llm_resp, 'result'):
            summary = llm_resp.result
            logger.info(f"[AI总评] 从result获取: {summary[:50] if summary else 'None'}...")

        # 方式3: text 属性
        if not summary and hasattr(llm_resp, 'text'):
            summary = llm_resp.text
            logger.info(f"[AI总评] 从text获取: {summary[:50] if summary else 'None'}...")

        # 方式4: 直接str
        if not summary:
            try:
                summary = str(llm_resp)
                if summary and len(summary) > 10:
                    logger.info(f"[AI总评] 从str获取: {summary[:50]}...")
            except Exception:
                pass

        # 方式5: 检查是否是字典
        if not summary and isinstance(llm_resp, dict):
            summary = llm_resp.get('completion_text') or llm_resp.get('result') or llm_resp.get('text')
            logger.info(f"[AI总评] 从dict获取: {summary[:50] if summary else 'None'}...")

        if summary and isinstance(summary, str) and summary.strip():
            logger.info(f"[AI总评] 生成成功: {summary[:50]}...")
            return summary.strip(), model_used, token_info

        logger.warning(f"[AI总评] 无法从LLM响应中获取有效文本，响应属性: {dir(llm_resp)}")
        return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", model_used, token_info

    except Exception as e:
        logger.error(f"[AI总评] 生成失败: {e}", exc_info=True)
        return "今天庭院也很热闹呢，大家都有在好好讨论游戏喵~", "", token_info


def _build_summary_prompt(posts: list[dict], window_no: str) -> str:
    """构建总结prompt"""
    lines = ["今日庭院社区（窗口 " + window_no + "）共 " + str(len(posts)) + " 个帖子，请总结："]

    for i, p in enumerate(posts[:10], 1):
        title = p.get('title', '(无标题)')
        comment = p.get('comment', '')[:80]
        lines.append(str(i) + ". 《" + title + "》 - " + comment)

    lines.append("请用可爱的语气总结今日社区氛围（50-100字）。")
    return "\n".join(lines)