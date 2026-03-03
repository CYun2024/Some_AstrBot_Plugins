"""
精简模型模块
处理解题输出的精简，支持直接用人设输出简化答案
"""
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter
from .utils import truncate_text


class Summarizer:
    """精简处理器"""
    
    def __init__(
        self,
        context: Context,
        debugger: DebuggerReporter,
        summarize_provider: str,
        summarize_model: str,
        solver_provider: str,
        solver_model: str
    ):
        self.context = context
        self.debugger = debugger
        self.summarize_provider = summarize_provider
        self.summarize_model = summarize_model
        self.solver_provider = solver_provider
        self.solver_model = solver_model
    
    def _get_provider_and_model(self) -> tuple[str, Optional[str]]:
        """获取精简模型的提供商和模型"""
        if self.summarize_provider:
            return self.summarize_provider, self.summarize_model
        return self.solver_provider, self.solver_model
    
    async def summarize(
        self,
        original_question: str,
        raw_answer: str,
        sender_info: dict,
        conv_id: str,
        persona_prompt: str = "",
        use_persona: bool = False
    ) -> Optional[str]:
        """
        精简解题输出
        
        Args:
            original_question: 原始问题
            raw_answer: 原始答案
            sender_info: 发送者信息
            conv_id: 会话ID
            persona_prompt: 人设提示词（当use_persona=True时使用）
            use_persona: 是否直接用人设输出（新功能）
        """
        provider_id, model_id = self._get_provider_and_model()
        
        if not provider_id:
            logger.warning("未配置精简模型，跳过精简")
            return None
        
        if use_persona and persona_prompt:
            # 新功能：直接用人设让精简模型输出简化答案
            logger.info(f"使用精简模型直接用人设输出: {provider_id}/{model_id or 'default'}")
            return await self._summarize_with_persona(
                original_question, raw_answer, sender_info, conv_id,
                provider_id, model_id, persona_prompt
            )
        else:
            # 传统精简模式
            logger.info(f"使用精简模型进行精简: {provider_id}/{model_id or 'default'}")
            return await self._summarize_traditional(
                original_question, raw_answer, sender_info, conv_id,
                provider_id, model_id
            )
    
    async def _summarize_traditional(
        self,
        original_question: str,
        raw_answer: str,
        sender_info: dict,
        conv_id: str,
        provider_id: str,
        model_id: Optional[str]
    ) -> Optional[str]:
        """传统精简模式"""
        summarize_prompt = f"""请对以下解答进行精简，要求：
1. 只保留核心步骤与简洁清晰的解释(为什么这么做等等)以及最终答案。
2. 保持所有数学公式（LaTeX）原样不变，例如 $...$ 或 $$...$$。
3. 最终输出应该是一个简洁、清晰的解答，便于直接阅读。

原始问题：{original_question}

原始解答：
{raw_answer}

精简后的解答（只保留核心）："""
        
        # 上报请求
        await self.debugger.report_request(
            provider_id=provider_id,
            model=model_id or "unknown",
            prompt=summarize_prompt,
            images=[],
            purpose="summarize",
            sender_info=sender_info,
            conv_id=conv_id
        )
        
        try:
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": summarize_prompt
            }
            if model_id:
                kwargs["model"] = model_id
            
            resp = await self.context.llm_generate(**kwargs)
            summarized = resp.completion_text.strip()
            
            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', model_id or 'unknown'),
                response=summarized,
                purpose="summarize",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )
            
            return summarized if summarized else raw_answer
        except Exception as e:
            logger.error(f"精简解题输出时调用模型失败: {e}")
            return raw_answer
    
    async def _summarize_with_persona(
        self,
        original_question: str,
        raw_answer: str,
        sender_info: dict,
        conv_id: str,
        provider_id: str,
        model_id: Optional[str],
        persona_prompt: str
    ) -> Optional[str]:
        """
        新功能：用人设让精简模型直接输出简化答案
        跳过主LLM的复述步骤，直接得到带人设的最终答案
        """
        summarize_prompt = f"""这是你的人设：{persona_prompt}

现在你需要用人设风格，对以下专业模型的解答进行精简并重新表述。要求：
1. 只保留核心步骤与简洁清晰的解释以及最终答案。
2. 保持所有数学公式（LaTeX）原样不变，例如 $...$ 或 $$...$$。
3. 用你的角色口吻重新组织语言，可以添加符合角色设定的语气词、表情符号等。
4. 最终输出应该是一个简洁、清晰、符合你人设的解答，便于直接阅读。
5. 你已经获得了完整解答，请直接复述精简后的内容，不要说你不会。

原始问题：{original_question}

专业模型解答：
{raw_answer}

用人设精简后的解答："""
        
        # 上报请求（purpose标记为summarize_persona以区分）
        await self.debugger.report_request(
            provider_id=provider_id,
            model=model_id or "unknown",
            prompt=summarize_prompt,
            images=[],
            purpose="summarize_with_persona",
            sender_info=sender_info,
            conv_id=conv_id
        )
        
        try:
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": summarize_prompt
            }
            if model_id:
                kwargs["model"] = model_id
            
            resp = await self.context.llm_generate(**kwargs)
            result = resp.completion_text.strip()
            
            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', model_id or 'unknown'),
                response=result,
                purpose="summarize_with_persona",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )
            
            logger.info("精简模型直接用人设输出成功")
            return result if result else raw_answer
        except Exception as e:
            logger.error(f"精简模型用人设输出失败: {e}")
            return raw_answer
