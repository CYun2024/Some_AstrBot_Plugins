"""
后处理模块 - 去除思考痕迹，保留完整内容
所有识图和解题结果都经过此模块处理，去除思考过程前缀，保留完整解答
"""
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter
from .utils import truncate_text


class Summarizer:
    """后处理器 - 去除思考痕迹，保留完整内容"""

    def __init__(
        self,
        context: Context,
        debugger: DebuggerReporter,
        summarize_provider: str,
        fallback_provider: str
    ):
        self.context = context
        self.debugger = debugger
        self.summarize_provider = summarize_provider
        self.fallback_provider = fallback_provider

    def _get_provider(self) -> str:
        """获取后处理模型提供商"""
        if self.summarize_provider:
            return self.summarize_provider
        return self.fallback_provider

    async def summarize(
        self,
        original_question: str,
        raw_content: str,
        sender_info: dict,
        conv_id: str,
        persona_prompt: str = "",
        use_persona: bool = False
    ) -> Optional[str]:
        """
        后处理内容 - 去除思考过程前缀，保留完整解题步骤
        
        Args:
            original_question: 原始问题
            raw_content: 原始内容（可能包含思考过程前缀的解题结果）
            sender_info: 发送者信息
            conv_id: 会话ID
            persona_prompt: 人设提示词
            use_persona: 是否用人设风格
        """
        provider_id = self._get_provider()
        
        if not provider_id:
            logger.warning("[Summarizer] 未配置后处理模型，返回原文")
            return self._remove_thinking_prefix(raw_content)

        # 构建后处理提示词 - 去除思考痕迹，保留完整内容
        base_prompt = f"""请对以下内容进行后处理。这是一个解题模型的输出，可能包含思考过程前缀（如"好的"、"让我思考一下"、"这个问题很有意思"等）。

【重要要求】
1. **去除思考前缀**：删除所有思考过程、自我对话、准备性陈述（如"我需要分析"、"首先让我们"、"好的我来解答"）
2. **保留完整解答**：保留所有具体的解题步骤、计算过程、公式推导、结论，一个字都不要省略
3. **保持结构**：保留原有的分段、列表、步骤编号
4. **公式保留**：如有数学公式，保留LaTeX格式 $...$ 或 $$...$$
5. **人设适配**：如果提供了人设，用该人设的语气重新组织语言，但内容必须完整保留，不要缩短

原始问题：{original_question}

原始内容：
{truncate_text(raw_content, 3000)}

请直接输出处理后的完整解答（去除思考前缀，保留全部步骤）："""

        if use_persona and persona_prompt:
            system_prompt = f"你是{persona_prompt}。请去除思考痕迹后完整复述解答。"
        else:
            system_prompt = "你是一个内容后处理器，专门去除AI的思考前缀，保留完整解答内容。"

        # 上报请求
        await self.debugger.report_request(
            provider_id=provider_id,
            model="default",
            prompt=base_prompt,
            images=[],
            purpose="content_cleanup",
            sender_info=sender_info,
            conv_id=conv_id,
            system_prompt=system_prompt,
            contexts=[]
        )

        try:
            logger.info(f"[Summarizer] 开始后处理，provider: {provider_id}")
            
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=base_prompt
            )
            
            processed = resp.completion_text.strip()
            
            # 如果处理结果为空或太短，回退到原文
            if len(processed) < len(raw_content) * 0.3:
                logger.warning(f"[Summarizer] 处理结果过短({len(processed)}字)，可能丢失了内容，回退到原文")
                processed = self._remove_thinking_prefix(raw_content)

            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                response=processed,
                purpose="content_cleanup",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )

            logger.info(f"[Summarizer] 后处理完成，原文{len(raw_content)}字 -> 处理后{len(processed)}字")
            return processed if processed else raw_content
            
        except Exception as e:
            logger.error(f"[Summarizer] 后处理失败: {e}")
            # 失败时返回去除前缀的原文
            return self._remove_thinking_prefix(raw_content)

    async def summarize_vision_result(
        self,
        original_question: str,
        vision_result: str,
        sender_info: dict,
        conv_id: str,
        persona_prompt: str = ""
    ) -> str:
        """
        专门用于处理后识图结果 - 去除思考痕迹，保留完整图片描述
        """
        provider_id = self._get_provider()
        
        if not provider_id:
            return self._remove_thinking_prefix(vision_result)

        prompt = f"""用户问："{original_question}"

识图模型原始输出：{vision_result}

请对识图结果进行后处理：
1. **去除思考前缀**：删除"我来分析一下"、"这张图片显示"等准备性陈述
2. **保留完整信息**：保留图片中所有的文字、数字、图表内容，不要省略
3. **结构清晰**：按用户问题组织答案，如果用户问"写了什么"，列出所有文字；如果问"有什么"，描述所有可见元素
4. **不要精简**：这不是摘要任务，请保留所有细节信息
5. **人设风格**：用人设风格输出：{persona_prompt or '助手'}

请输出完整的处理后内容（保留所有细节）："""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text.strip()
            if len(result) < len(vision_result) * 0.5:
                # 如果结果太短，可能过度精简了，回退
                logger.warning("[Summarizer] 识图结果处理后被过度精简，回退到原文")
                return self._remove_thinking_prefix(vision_result)
            return result
        except Exception as e:
            logger.error(f"[Summarizer] 识图后处理失败: {e}")
            return self._remove_thinking_prefix(vision_result)

    def _remove_thinking_prefix(self, text: str) -> str:
        """
        规则去除常见的思考前缀
        """
        if not text:
            return text
            
        # 常见的思考前缀模式
        prefixes = [
            r'^好的[，,]\s*',
            r'^让我(来|先)?(思考|分析|看看|想想|研究)(一下|看|看)[，,]?\s*',
            r'^这个问题(很|挺)(有意思|有趣|值得讨论)[，,]?\s*',
            r'^我需要(先|仔细)?(分析|思考|理解)(一下)?[，,]?\s*',
            r'^首先[，,]\s*',
            r'^我来(为你|帮您|给大家)?(解答|分析|解释)(一下)?[，,]?\s*',
            r'^根据(题目|问题|图片)[，,]\s*',
            r'^从(图片|题目|问题)(中|可以|看出)[，,]?\s*',
        ]
        
        import re
        result = text
        for pattern in prefixes:
            result = re.sub(pattern, '', result, flags=re.IGNORECASE | re.MULTILINE)
        
        return result.strip()