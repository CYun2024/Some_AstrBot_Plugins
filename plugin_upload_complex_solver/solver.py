"""
解题模型
"""
import asyncio
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter
from .utils import build_history_messages


class Solver:
    def __init__(self, context: Context, debugger: DebuggerReporter,
                 solver_provider: str, solver_model: str,
                 solver_provider_2: str, solver_model_2: str,
                 max_wait_minutes: int, enable_context: bool):
        self.context = context
        self.debugger = debugger
        self.solver_provider = solver_provider
        self.solver_provider_2 = solver_provider_2
        self.max_wait_minutes = max_wait_minutes
        self.enable_context = enable_context
        self.round_timeout = 180  # 3分钟

    async def solve_with_retry(self, question: str, images: List[str], 
                               history_messages: List, sender_info: dict,
                               conv_id: str, waiting_callback=None):
        """解题 - 增加了问题明确性检查"""
        # 检查问题明确性
        clarity_check = self._check_question_clarity(question)
        if not clarity_check["is_clear"]:
            return f"[问题不明确] {clarity_check['reason']}", True
        
        full_prompt = self._build_solver_prompt(question, history_messages)
        max_rounds = max(1, (self.max_wait_minutes + 2) // 3)
        
        for round_num in range(max_rounds):
            providers = []
            if self.solver_provider:
                providers.append(self.solver_provider)
            if self.solver_provider_2:
                providers.append(self.solver_provider_2)
            
            if not providers:
                return None, False
            
            pending_tasks = set()
            for provider_id in providers:
                task = asyncio.create_task(
                    self._call_solver(provider_id, full_prompt, images, 
                                    sender_info, conv_id)
                )
                pending_tasks.add(task)
            
            # 等待任意完成
            try:
                while pending_tasks:
                    done, pending_tasks = await asyncio.wait(
                        pending_tasks, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        result = task.result()
                        if result and len(result) > 10:
                            for t in pending_tasks:
                                t.cancel()
                            return result, True
            except Exception as e:
                logger.error(f"[Solver] 轮次错误: {e}")
        
        return None, False

    def _check_question_clarity(self, question: str) -> dict:
        """
        快速检查问题是否明确
        返回: {"is_clear": bool, "reason": str}
        """
        stripped = question.strip()
        
        # 过短
        if len(stripped) < 5:
            return {
                "is_clear": False, 
                "reason": "问题描述过短（少于5个字符），请提供完整的问题描述，例如具体的题目内容或计算需求。"
            }
        
        # 只有标点
        import re
        if all(c in ' \t\n\r\?\！\。\，\.\,\!\;\；\:\"\'\(\)\（\）\[\]\【\】\-' for c in stripped):
            return {
                "is_clear": False,
                "reason": "问题只包含标点符号，请详细描述您需要解答的具体内容。"
            }
        
        # 明显的不完整（以这些词结尾，后面没有内容）
        incomplete_patterns = [
            r'这道题\s*$',
            r'这个题\s*$',
            r'求解\s*$',
            r'解答\s*$',
            r'怎么做\s*$',
            r'答案\s*$',
            r'问题\s*$',
        ]
        for pattern in incomplete_patterns:
            if re.search(pattern, stripped, re.IGNORECASE):
                return {
                    "is_clear": False,
                    "reason": "问题描述不完整（\"" + stripped[-10:] + "\"），请提供完整的题目内容或具体的问题描述。"
                }
        
        # 检查是否只是引用+简短词（可能是追问）
        if len(stripped) < 15 and ('引用' in stripped or '回复' in stripped):
            return {
                "is_clear": False,
                "reason": "问题似乎是对之前消息的简短引用，请提供完整的问题描述。"
            }
        
        return {"is_clear": True, "reason": ""}

    async def _call_solver(self, provider_id: str, prompt: str, images: List[str],
                          sender_info: dict, conv_id: str) -> Optional[str]:
        """调用单个解题模型"""
        try:
            kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
            if images:
                kwargs["image_urls"] = images
            
            resp = await self.context.llm_generate(**kwargs)
            return resp.completion_text
            
        except Exception as e:
            logger.error(f"[Solver] {provider_id} 失败: {e}")
            return None

    def _build_solver_prompt(self, question: str, history_messages: List) -> str:
        messages = build_history_messages(history_messages, self.enable_context)
        system_prompt = """你是专业解题助手，给出详细准确的解答。

【重要规则】
1. 直接给出解答，不要输出思考过程（如"我需要分析"、"让我思考"等前缀）
2. 给出完整的解题步骤，不要省略中间过程
3. 数学公式使用LaTeX格式
4. 如果问题不明确或缺少必要信息，请说明无法解答的原因"""
        messages.insert(0, f"system: {system_prompt}")
        messages.append(f"user: {question}")
        return "\n".join(messages)