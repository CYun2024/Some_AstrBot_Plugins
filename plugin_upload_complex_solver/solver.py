"""
解题模型模块
处理复杂问题的求解，支持双模型并行调用
"""
import asyncio
from typing import List, Optional, Dict, Any

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter
from .utils import build_history_messages


class Solver:
    """解题处理器"""
    
    def __init__(
        self,
        context: Context,
        debugger: DebuggerReporter,
        solver_provider: str,
        solver_model: str,
        solver_provider_2: str,
        solver_model_2: str,
        max_wait_minutes: int,
        enable_context: bool
    ):
        self.context = context
        self.debugger = debugger
        self.solver_provider = solver_provider
        self.solver_model = solver_model
        self.solver_provider_2 = solver_provider_2
        self.solver_model_2 = solver_model_2
        self.max_wait_minutes = max_wait_minutes
        self.enable_context = enable_context
        
        # 每次等待时间（3分钟 = 180秒）
        self.round_timeout = 180
    
    async def solve_with_retry(
        self,
        question: str,
        images: List[str],
        history_messages: List,
        sender_info: dict,
        conv_id: str,
        waiting_callback = None
    ) -> tuple[Optional[str], bool]:
        """
        调用双模型求解，支持超时重试
        
        Args:
            question: 问题
            images: 图片列表
            history_messages: 历史消息
            sender_info: 发送者信息
            conv_id: 会话ID
            waiting_callback: 等待回调函数，用于发送等待消息
            
        Returns:
            (答案, 是否成功)
        """
        full_prompt = self._build_solver_prompt(question, history_messages)
        
        result_lock = asyncio.Lock()
        result_container = {"answer": None, "done": False}
        
        max_rounds = max(1, (self.max_wait_minutes + 2) // 3)
        
        for round_num in range(max_rounds):
            logger.info(f"开始第 {round_num + 1}/{max_rounds} 轮解题调用")
            
            providers = []
            if self.solver_provider:
                providers.append((self.solver_provider, self.solver_model, "solver_1"))
            if self.solver_provider_2:
                providers.append((self.solver_provider_2, self.solver_model_2, "solver_2"))
            
            if not providers:
                logger.error("没有配置任何解题模型")
                return None, False
            
            tasks = []
            for provider_id, model_id, solver_tag in providers:
                task = asyncio.create_task(
                    self._call_single_solver(
                        provider_id, model_id, full_prompt, images,
                        sender_info, conv_id, solver_tag, result_lock, result_container
                    )
                )
                tasks.append(task)
            
            # 发送等待消息（非第一轮）
            if round_num > 0 and waiting_callback:
                try:
                    await waiting_callback(round_num)
                    logger.info(f"已发送第 {round_num} 轮等待提示")
                except Exception as e:
                    logger.error(f"发送等待消息失败: {e}")
            
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=self.round_timeout,
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # 取消未完成的任务
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                # 检查结果
                async with result_lock:
                    if result_container["done"] and result_container["answer"]:
                        logger.info(f"第 {round_num + 1} 轮成功获取到答案")
                        return result_container["answer"], True
                
                # 处理完成的任务
                for task in done:
                    try:
                        result = task.result()
                        if result:
                            async with result_lock:
                                if not result_container["done"]:
                                    result_container["answer"] = result
                                    result_container["done"] = True
                                    return result, True
                    except Exception as e:
                        logger.debug(f"任务异常结束: {e}")
                        continue
                
                logger.warning(f"第 {round_num + 1} 轮未能在3分钟内获取有效答案")
                
            except asyncio.TimeoutError:
                logger.warning(f"第 {round_num + 1} 轮完全超时（3分钟）")
                for task in tasks:
                    if not task.done():
                        task.cancel()
            except Exception as e:
                logger.error(f"第 {round_num + 1} 轮发生错误: {e}")
                for task in tasks:
                    if not task.done():
                        task.cancel()
        
        logger.error(f"经过 {max_rounds} 轮尝试（约{self.max_wait_minutes}分钟），仍未能获取有效答案")
        return None, False
    
    async def _call_single_solver(
        self,
        provider_id: str,
        model_id: str,
        prompt: str,
        images: List[str],
        sender_info: dict,
        conv_id: str,
        solver_tag: str,
        result_lock: asyncio.Lock,
        result_container: dict
    ) -> Optional[str]:
        """调用单个解题模型"""
        # 检查是否已有结果
        async with result_lock:
            if result_container["done"]:
                logger.debug(f"{solver_tag}: 已有其他模型返回结果，取消调用")
                return None
        
        # 上报请求
        await self.debugger.report_request(
            provider_id=provider_id,
            model=model_id or "unknown",
            prompt=prompt,
            images=images,
            purpose=f"solver_call_{solver_tag}",
            sender_info=sender_info,
            conv_id=conv_id
        )
        
        try:
            logger.info(f"[{solver_tag}] 开始调用: {provider_id}")
            
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
                "image_urls": images
            }
            if model_id:
                kwargs["model"] = model_id
            
            resp = await self.context.llm_generate(**kwargs)
            result = resp.completion_text
            
            if not result or len(result.strip()) < 5:
                logger.warning(f"[{solver_tag}] 返回结果过短，视为无效")
                return None
            
            logger.info(f"[{solver_tag}] 成功获取响应，长度: {len(result)}")
            
            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', model_id or 'unknown'),
                response=result,
                purpose=f"solver_call_{solver_tag}",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )
            
            # 设置结果
            async with result_lock:
                if not result_container["done"]:
                    result_container["answer"] = result
                    result_container["done"] = True
                    logger.info(f"[{solver_tag}] 成功设置结果为有效答案")
                    return result
                else:
                    logger.info(f"[{solver_tag}] 返回了结果，但已有其他模型先返回，忽略")
                    return None
                    
        except Exception as e:
            logger.error(f"[{solver_tag}] 调用失败: {e}")
            return None
    
    def _build_solver_prompt(self, question: str, history_messages: List) -> str:
        """构建解题提示词"""
        messages = build_history_messages(history_messages, self.enable_context)
        messages.append(f"user: {question}")
        return "\n".join(messages)
