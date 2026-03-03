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
        修复：正确处理asyncio.wait，确保任意模型成功立即返回
        """
        full_prompt = self._build_solver_prompt(question, history_messages)

        result_container = {"answer": None, "done": False}
        result_lock = asyncio.Lock()

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

            # 创建任务
            pending_tasks = set()
            for provider_id, model_id, solver_tag in providers:
                task = asyncio.create_task(
                    self._call_single_solver(
                        provider_id, model_id, full_prompt, images,
                        sender_info, conv_id, solver_tag, result_lock, result_container
                    ),
                    name=solver_tag
                )
                pending_tasks.add(task)

            # 发送等待消息（非第一轮）
            if round_num > 0 and waiting_callback:
                try:
                    await waiting_callback(round_num)
                    logger.info(f"已发送第 {round_num} 轮等待提示")
                except Exception as e:
                    logger.error(f"发送等待消息失败: {e}")

            # 【关键修复】使用更简单的超时逻辑
            start_time = asyncio.get_event_loop().time()
            timeout_at = start_time + self.round_timeout
            
            try:
                while pending_tasks and asyncio.get_event_loop().time() < timeout_at:
                    # 等待任意一个任务完成
                    remaining = timeout_at - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    
                    # 等待第一个完成的任务，最多等1秒
                    done, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        timeout=min(remaining, 1.0),
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    # 处理完成的任务
                    for task in done:
                        try:
                            result = task.result()
                            if result and len(result.strip()) > 10:  # 有效结果
                                logger.info(f"[{task.get_name()}] 获取到有效结果，长度: {len(result)}")
                                # 取消其他pending任务
                                for pending_task in pending_tasks:
                                    pending_task.cancel()
                                # 等待取消完成
                                if pending_tasks:
                                    await asyncio.wait(pending_tasks, timeout=2.0)
                                return result, True
                            else:
                                logger.warning(f"[{task.get_name()}] 返回结果过短或为空，继续等待")
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            logger.warning(f"[{task.get_name()}] 异常: {e}")
                
                # 本轮结束，取消剩余任务
                for task in pending_tasks:
                    if not task.done():
                        task.cancel()
                if pending_tasks:
                    await asyncio.wait(pending_tasks, timeout=2.0)
                
                # 检查是否超时
                if asyncio.get_event_loop().time() >= timeout_at:
                    logger.warning(f"第 {round_num + 1} 轮超时（{self.round_timeout}秒）")
                else:
                    logger.warning(f"第 {round_num + 1} 轮所有模型都失败或无有效返回")
                    
            except Exception as e:
                logger.error(f"第 {round_num + 1} 轮发生错误: {e}")
                for task in pending_tasks:
                    task.cancel()

        logger.error(f"经过 {max_rounds} 轮尝试，仍未能获取有效答案")
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
                logger.debug(f"[{solver_tag}] 已有结果，跳过执行")
                return None

        logger.info(f"[{solver_tag}] 准备调用解题模型")
        logger.info(f"[{solver_tag}] 提供商: {provider_id}, 模型: {model_id or 'default'}")
        logger.debug(f"[{solver_tag}] Prompt长度: {len(prompt)} 字符")
        logger.debug(f"[{solver_tag}] 图片数量: {len(images)}")

        try:
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
            }
            if images:
                kwargs["image_urls"] = images
            if model_id:
                kwargs["model"] = model_id

            logger.info(f"[{solver_tag}] 开始调用 API...")
            start_time = asyncio.get_event_loop().time()
            
            resp = await self.context.llm_generate(**kwargs)
            result = resp.completion_text
            
            elapsed = asyncio.get_event_loop().time() - start_time
            logger.info(f"[{solver_tag}] API调用完成，耗时: {elapsed:.1f}秒")

            if not result or len(result.strip()) < 10:
                logger.warning(f"[{solver_tag}] 返回结果过短({len(result) if result else 0}字符)，视为无效")
                return None

            logger.info(f"[{solver_tag}] 成功获取有效结果，长度: {len(result)}")
            return result

        except Exception as e:
            logger.error(f"[{solver_tag}] 调用失败: {e}")
            return None

    def _build_solver_prompt(self, question: str, history_messages: List) -> str:
        """构建解题提示词"""
        messages = build_history_messages(history_messages, self.enable_context)
        
        # 【关键】如果问题包含图片描述，添加特殊说明
        if "[图片内容描述：" in question:
            system_prompt = """你是一个专业的解题助手，擅长解决数学、物理、化学、编程等复杂问题。

【重要提示】
- 用户发送了图片，图片内容已转换为文字描述（见[图片内容描述]部分）
- 用户问题中可能包含"韶梦"这个词，这是Bot的名字，**不是图片里的内容**

请给出详细、准确的解答。"""
        else:
            system_prompt = "你是一个专业的解题助手，擅长解决数学、物理、化学、编程等复杂问题。请给出详细、准确的解答。"
        
        messages.insert(0, f"system: {system_prompt}")
        messages.append(f"user: {question}")
        
        return "\n".join(messages)