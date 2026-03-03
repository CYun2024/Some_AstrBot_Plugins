"""
AstrBot 复杂问题求解助手插件
通过强弱模型协作，自动分流复杂问题，人设化复述解答，支持多模态和LaTeX渲染。
支持双模型并行调用、超时重试机制、独立精简模型。
"""

import re
import io
import hashlib
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@register(
    name="complex_solver",
    author="CYun2024",
    desc="多模型协作求解插件，自动识别复杂问题并调用专业模型，人设化复述解题过程，支持双模型并行",
    version="3.0.0",
    repo="https://github.com/CYun2024"
)
class ComplexSolverPlugin(Star):
    """复杂问题求解助手 - 双模型并行版"""

    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context

        config = config or {}

        # 解题模型1配置
        self.solver_provider = config.get("solver_provider", "")
        self.solver_model = config.get("solver_model", "")

        # 解题模型2配置（新增）
        self.solver_provider_2 = config.get("solver_provider_2", "")
        self.solver_model_2 = config.get("solver_model_2", "")

        # 精简模型配置（新增）
        self.summarize_provider = config.get("summarize_provider", "")
        self.summarize_model = config.get("summarize_model", "")

        # 其他配置
        self.enable_latex = config.get("enable_latex_render", True)
        self.enable_context = config.get("enable_context", True)
        self.context_timeout = config.get("context_timeout", 600)
        self.enable_summarize = config.get("enable_summarize", True)
        self.max_wait_minutes = config.get("max_wait_minutes", 9)

        # 每次等待时间（3分钟 = 180秒）
        self.round_timeout = 180

        self.img_dir = Path(get_astrbot_data_path()) / "plugin_data" / "complex_solver" / "images"
        self.img_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"ComplexSolverPlugin v3.0 已加载，双模型模式: {bool(self.solver_provider_2)}")

    async def _report_to_debugger(self, data: dict):
        """尝试获取 LLM Debugger 实例并上报调用记录"""
        try:
            debugger = self.context.get_registered_star("llm_debugger")
            if debugger and hasattr(debugger, 'record_llm_call'):
                await debugger.record_llm_call(data)
        except Exception as e:
            logger.debug(f"上报给 LLM Debugger 失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def handle_message(self, event: AstrMessageEvent):
        message = event.message_str.strip()
        if not message:
            return

        images = self._extract_images(event)
        umo = event.unified_msg_origin

        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)

        sender_info = {
            "id": event.get_sender_id(),
            "name": event.get_sender_name(),
            "group_id": event.get_group_id() if hasattr(event, 'get_group_id') else None,
            "platform": event.get_platform_name()
        }

        main_provider_id = await self.context.get_current_chat_provider_id(umo)
        if not main_provider_id:
            logger.warning("无法获取当前会话的主模型ID")
            return

        try:
            is_complex = await self._is_complex_question(main_provider_id, message, images, sender_info, curr_cid)
        except Exception as e:
            logger.error(f"判断复杂问题时出错: {e}")
            return

        if not is_complex and self.enable_context:
            try:
                is_followup = await self._is_followup_question(main_provider_id, umo, message, sender_info, curr_cid)
                if is_followup:
                    is_complex = True
            except Exception as e:
                logger.error(f"判断追问时出错: {e}")

        if not is_complex:
            return

        # 检查是否有配置解题模型
        if not self.solver_provider and not self.solver_provider_2:
            apology = await self._generate_apology(main_provider_id, "解题服务未配置，请管理员配置解题模型", message, sender_info, curr_cid)
            await event.send(event.plain_result(apology))
            event.stop_event()
            return

        history_messages = []
        if curr_cid:
            try:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation:
                    history_messages = conversation.history or []
            except Exception as e:
                logger.warning(f"获取历史对话失败: {e}")

        # 调用双模型求解（带重试机制）
        solver_answer, success = await self._call_solver_with_retry(
            event, message, images, history_messages, sender_info, curr_cid, main_provider_id
        )

        if not success or not solver_answer:
            apology = await self._generate_apology(
                main_provider_id, 
                f"解题助手在 {self.max_wait_minutes} 分钟内未能获取有效解答，请稍后重试或简化问题", 
                message, sender_info, curr_cid
            )
            await event.send(event.plain_result(apology))
            event.stop_event()
            return

        # 使用独立精简模型进行精简
        if self.enable_summarize:
            try:
                summarized = await self._summarize_solver_output(
                    message, solver_answer, sender_info, curr_cid
                )
                if summarized:
                    solver_answer = summarized
                    logger.info("解题输出精简成功")
            except Exception as e:
                logger.error(f"精简解题输出时出错: {e}，使用原答案")

        # 人设复述
        try:
            restated = await self._persona_restate(main_provider_id, solver_answer, message, sender_info, curr_cid)
        except Exception as e:
            logger.error(f"人设复述失败: {e}，直接返回原答案")
            restated = solver_answer

        # 发送结果
        try:
            if self.enable_latex:
                await self._send_with_latex(event, restated)
            else:
                await event.send(event.plain_result(restated))
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            await event.send(event.plain_result(restated))

        # 保存对话历史
        if curr_cid and self.enable_context:
            try:
                from astrbot.core.agent.message import UserMessageSegment, AssistantMessageSegment, TextPart
                user_msg = UserMessageSegment(content=[TextPart(text=message)])
                assistant_msg = AssistantMessageSegment(content=[TextPart(text=solver_answer)])
                await conv_mgr.add_message_pair(curr_cid, user_msg, assistant_msg)
            except Exception as e:
                logger.warning(f"保存对话历史失败: {e}")

        event.stop_event()

    async def _call_solver_with_retry(
        self, event: AstrMessageEvent, question: str, images: List[str], 
        history_messages: List, sender_info: dict, conv_id: str, main_provider_id: str
    ) -> tuple[Optional[str], bool]:
        """
        调用双模型求解，支持超时重试
        返回: (答案, 是否成功)
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

            if round_num > 0:
                try:
                    waiting_msg = await self._generate_waiting_message(
                        main_provider_id, round_num, question, sender_info, conv_id
                    )
                    await event.send(event.plain_result(waiting_msg))
                    logger.info(f"已发送第 {round_num} 轮等待提示")
                except Exception as e:
                    logger.error(f"发送等待消息失败: {e}")

            try:
                done, pending = await asyncio.wait(
                    tasks, 
                    timeout=self.round_timeout,
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                async with result_lock:
                    if result_container["done"] and result_container["answer"]:
                        logger.info(f"第 {round_num + 1} 轮成功获取到答案")
                        return result_container["answer"], True

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
        self, provider_id: str, model_id: str, prompt: str, images: List[str],
        sender_info: dict, conv_id: str, solver_tag: str,
        result_lock: asyncio.Lock, result_container: dict
    ) -> Optional[str]:
        """调用单个解题模型"""
        async with result_lock:
            if result_container["done"]:
                logger.debug(f"{solver_tag}: 已有其他模型返回结果，取消调用")
                return None

        req_data = {
            "phase": "request",
            "provider_id": provider_id,
            "model": model_id or "unknown",
            "prompt": prompt[:500] + "..." if len(prompt) > 500 else prompt,
            "images": images,
            "source": {"plugin": "complex_solver", "purpose": f"solver_call_{solver_tag}"},
            "sender": sender_info,
            "conversation_id": conv_id,
            "timestamp": datetime.now().isoformat()
        }
        await self._report_to_debugger(req_data)

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

            resp_data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": getattr(resp, 'model', model_id or 'unknown'),
                "response": result[:200] + "..." if len(result) > 200 else result,
                "usage": getattr(resp, 'usage', None),
                "source": {"plugin": "complex_solver", "purpose": f"solver_call_{solver_tag}"},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat()
            }
            await self._report_to_debugger(resp_data)

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
        messages = []

        if history_messages and self.enable_context:
            for msg in history_messages[-4:]:
                try:
                    if isinstance(msg, dict):
                        role = msg.get('role', 'user')
                        content = msg.get('content', '')
                    elif hasattr(msg, 'role') and hasattr(msg, 'content'):
                        role = msg.role
                        content = msg.content
                        if isinstance(content, list) and len(content) > 0:
                            text_parts = []
                            for part in content:
                                if hasattr(part, 'text'):
                                    text_parts.append(part.text)
                                elif isinstance(part, dict) and 'text' in part:
                                    text_parts.append(part['text'])
                            content = ''.join(text_parts)
                    elif isinstance(msg, str):
                        role = 'user'
                        content = msg
                    else:
                        continue

                    messages.append(f"{role}: {str(content)}")
                except Exception as e:
                    logger.debug(f"处理历史消息时出错: {e}")
                    continue

        messages.append(f"user: {question}")
        return "\n".join(messages)

    async def _generate_waiting_message(
        self, provider_id: str, round_num: int, question: str, sender_info: dict, conv_id: str
    ) -> str:
        """生成等待提示消息"""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            if isinstance(persona, dict):
                persona_prompt = persona.get('prompt', "一个乐于助人的助手")
            else:
                persona_prompt = getattr(persona, 'prompt', "一个乐于助人的助手") if persona else "一个乐于助人的助手"
        except Exception:
            persona_prompt = "一个乐于助人的助手"

        prompt = f"""你是一个{persona_prompt}。用户提出了一个复杂问题，正在等待解题模型返回结果（已经等待了 {round_num * 3} 分钟）。
请用你的角色风格向用户说明正在努力思考中，可能需要再等待一段时间，语气要温和、有礼貌。不要回答原问题，而是表示抱歉。

请生成一段简短的等待提示（30字内）："""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.error(f"生成等待消息失败: {e}")
            return f"[思考中...已等待{round_num * 3}分钟，请稍候，正在努力解决您的问题]"

    def _extract_images(self, event: AstrMessageEvent) -> List[str]:
        try:
            return event.get_images()
        except AttributeError:
            return []

    async def _is_complex_question(self, provider_id: str, question: str, images: List[str], sender_info: dict, conv_id: str) -> bool:
        prompt = """请判断以下用户问题是否属于复杂问题，需要调用强大的专业模型来解决。
复杂问题包括但不限于：数学计算、逻辑推理、代码编写、专业学科问答、需要多步推理的问题。
如果问题是日常闲聊、简单问候、情感表达等，则不属于复杂问题。

用户问题：{question}

请只输出"COMPLEX"或"SIMPLE"，不要输出其他内容。""".format(question=question)

        req_data = {
            "phase": "request",
            "provider_id": provider_id,
            "model": "unknown",
            "prompt": prompt,
            "images": images,
            "source": {"plugin": "complex_solver", "purpose": "complexity_judge"},
            "sender": sender_info,
            "conversation_id": conv_id,
            "timestamp": datetime.now().isoformat()
        }
        await self._report_to_debugger(req_data)

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=images
            )
            result = resp.completion_text.strip().upper() == "COMPLEX"

            resp_data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": getattr(resp, 'model', 'unknown'),
                "response": resp.completion_text,
                "usage": getattr(resp, 'usage', None),
                "source": {"plugin": "complex_solver", "purpose": "complexity_judge"},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat()
            }
            await self._report_to_debugger(resp_data)

            return result
        except Exception as e:
            logger.error(f"复杂问题判断失败: {e}")
            return False

    async def _is_followup_question(self, provider_id: str, umo: str, question: str, sender_info: dict, conv_id: str) -> bool:
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        if not curr_cid:
            return False

        try:
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
        except Exception as e:
            logger.error(f"获取对话失败: {e}")
            return False

        if not conversation or not conversation.history:
            return False

        last_user = last_assistant = None

        for msg in reversed(conversation.history):
            try:
                if isinstance(msg, dict):
                    role = msg.get('role')
                    content = msg.get('content')
                elif hasattr(msg, 'role') and hasattr(msg, 'content'):
                    role = msg.role
                    content = msg.content
                    if isinstance(content, list) and len(content) > 0:
                        text_parts = []
                        for part in content:
                            if hasattr(part, 'text'):
                                text_parts.append(part.text)
                            elif isinstance(part, dict) and 'text' in part:
                                text_parts.append(part['text'])
                        content = ''.join(text_parts)
                elif isinstance(msg, str):
                    continue
                else:
                    continue

                if role == 'user' and last_user is None:
                    last_user = str(content) if content else ""
                elif role == 'assistant' and last_assistant is None:
                    last_assistant = str(content) if content else ""

                if last_user and last_assistant:
                    break

            except Exception as e:
                logger.debug(f"解析历史消息时出错: {e}")
                continue

        if not last_user or not last_assistant:
            return False

        prompt = f"""以下是之前用户提出的复杂问题和专业模型的解答。
现在用户又发送了一条新消息。请判断这条新消息是否是对之前问题的追问
（例如请求解释某一步、询问原因等）。如果是，输出"FOLLOWUP"，否则输出"NEW"。

之前用户问题：{last_user}
之前解答：{last_assistant}

新消息：{question}

请只输出"FOLLOWUP"或"NEW"。"""

        req_data = {
            "phase": "request",
            "provider_id": provider_id,
            "model": "unknown",
            "prompt": prompt,
            "images": [],
            "source": {"plugin": "complex_solver", "purpose": "followup_judge"},
            "sender": sender_info,
            "conversation_id": conv_id,
            "timestamp": datetime.now().isoformat()
        }
        await self._report_to_debugger(req_data)

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text.strip().upper() == "FOLLOWUP"

            resp_data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": getattr(resp, 'model', 'unknown'),
                "response": resp.completion_text,
                "usage": getattr(resp, 'usage', None),
                "source": {"plugin": "complex_solver", "purpose": "followup_judge"},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat()
            }
            await self._report_to_debugger(resp_data)

            return result
        except Exception as e:
            logger.error(f"追问判断失败: {e}")
            return False

    async def _summarize_solver_output(
        self, original_question: str, raw_answer: str, sender_info: dict, conv_id: str
    ) -> Optional[str]:
        """使用配置的独立精简模型进行精简"""
        provider_id = self.summarize_provider or self.solver_provider
        if not provider_id:
            logger.warning("未配置精简模型，跳过精简")
            return None

        if self.summarize_provider:
            model_id = self.summarize_model
            logger.info(f"使用独立精简模型: {provider_id}/{model_id or 'default'}")
        else:
            model_id = self.solver_model
            logger.info(f"使用解题模型1进行精简: {provider_id}/{model_id or 'default'}")

        summarize_prompt = f"""请对以下解答进行精简，要求：
1. 只保留核心步骤与简洁清晰的解释(为什么这么做等等)以及最终答案。
2. 保持所有数学公式（LaTeX）原样不变，例如 $...$ 或 $$...$$。
3. 最终输出应该是一个简洁、清晰的解答，便于直接阅读。

原始问题：{original_question}

原始解答：
{raw_answer}

精简后的解答（只保留核心）："""

        req_data = {
            "phase": "request",
            "provider_id": provider_id,
            "model": model_id or "unknown",
            "prompt": summarize_prompt[:500] + "..." if len(summarize_prompt) > 500 else summarize_prompt,
            "images": [],
            "source": {"plugin": "complex_solver", "purpose": "summarize"},
            "sender": sender_info,
            "conversation_id": conv_id,
            "timestamp": datetime.now().isoformat()
        }
        await self._report_to_debugger(req_data)

        try:
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": summarize_prompt
            }
            if model_id:
                kwargs["model"] = model_id

            resp = await self.context.llm_generate(**kwargs)
            summarized = resp.completion_text.strip()

            resp_data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": getattr(resp, 'model', model_id or 'unknown'),
                "response": summarized[:200] + "..." if len(summarized) > 200 else summarized,
                "usage": getattr(resp, 'usage', None),
                "source": {"plugin": "complex_solver", "purpose": "summarize"},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat()
            }
            await self._report_to_debugger(resp_data)

            return summarized if summarized else raw_answer
        except Exception as e:
            logger.error(f"精简解题输出时调用模型失败: {e}")
            return raw_answer

    async def _persona_restate(self, provider_id: str, text: str, original_question: str, sender_info: dict, conv_id: str) -> str:
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            if isinstance(persona, dict):
                persona_prompt = persona.get('prompt', "一只猫娘助手")
            else:
                persona_prompt = getattr(persona, 'prompt', "一只猫娘助手") if persona else "一只猫娘助手"
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}，使用默认")
            persona_prompt = "一只猫娘助手"

        prompt = f"""这是你的人设:{persona_prompt}。现在你需要用你的角色风格重新表述下面专业模型给出的解答。要求：
1. 直接一模一样复述答案，在语句末尾加上简单的口癖（比如喵~）也是可行的。你已经获得了回答，请不要说你不会。如果你感觉解答是乱码也请直接一字不落的复述。
2. 如果你能力较强，可以转述回答，严格保留解答的逻辑、步骤和正确性，不得修改任何数学公式、推理步骤。
3. 用你的角色口吻重新组织语言，可以添加符合角色设定的语气词、表情符号等。
4. 如果解答中包含LaTeX数学公式，请保留原样（例如$...$或$$...$$），不要修改。

用户问题：{original_question}

专业模型解答：
{text}

"""

        req_data = {
            "phase": "request",
            "provider_id": provider_id,
            "model": "unknown",
            "prompt": prompt,
            "images": [],
            "source": {"plugin": "complex_solver", "purpose": "persona_restate"},
            "sender": sender_info,
            "conversation_id": conv_id,
            "timestamp": datetime.now().isoformat()
        }
        await self._report_to_debugger(req_data)

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text

            resp_data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": getattr(resp, 'model', 'unknown'),
                "response": result,
                "usage": getattr(resp, 'usage', None),
                "source": {"plugin": "complex_solver", "purpose": "persona_restate"},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat()
            }
            await self._report_to_debugger(resp_data)

            return result
        except Exception as e:
            logger.error(f"人设复述失败: {e}")
            return text

    async def _generate_apology(self, provider_id: str, reason: str, original_question: str, sender_info: dict, conv_id: str) -> str:
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            if isinstance(persona, dict):
                persona_prompt = persona.get('prompt', "一个乐于助人的助手")
            else:
                persona_prompt = getattr(persona, 'prompt', "一个乐于助人的助手") if persona else "一个乐于助人的助手"
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}，使用默认")
            persona_prompt = "一个乐于助人的助手"

        prompt = f"""你是一个{persona_prompt}。用户刚才提出了一个复杂问题，但由于技术原因暂时无法解答。
请用你的角色风格向用户表达歉意，并说明原因（{reason}），不要尝试回答问题或提供解答。

用户问题：{original_question}

请用你的角色风格说一些抱歉的话："""

        req_data = {
            "phase": "request",
            "provider_id": provider_id,
            "model": "unknown",
            "prompt": prompt,
            "images": [],
            "source": {"plugin": "complex_solver", "purpose": "apology"},
            "sender": sender_info,
            "conversation_id": conv_id,
            "timestamp": datetime.now().isoformat()
        }
        await self._report_to_debugger(req_data)

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text

            resp_data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": getattr(resp, 'model', 'unknown'),
                "response": result,
                "usage": getattr(resp, 'usage', None),
                "source": {"plugin": "complex_solver", "purpose": "apology"},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat()
            }
            await self._report_to_debugger(resp_data)

            return result
        except Exception as e:
            logger.error(f"生成道歉消息失败: {e}")
            return f"抱歉，{reason}"

    def _render_latex(self, formula: str, display_mode: bool = False) -> bytes:
        try:
            formula = formula.strip()

            plt.figure(figsize=(0.01, 0.01))
            if display_mode:
                tex = f"$${formula}$$"
            else:
                tex = f"${formula}$"

            plt.text(0.5, 0.5, tex, fontsize=12, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', pad_inches=0.1)
            plt.close()
            buf.seek(0)
            return buf.read()
        except Exception as e:
            logger.error(f"LaTeX 渲染失败: {e}")
            plt.figure(figsize=(2, 1))
            plt.text(0.5, 0.5, "[公式渲染失败]", fontsize=10, ha='center', va='center')
            plt.axis('off')
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            return buf.read()

    def _process_latex(self, text: str) -> List[Dict[str, Any]]:
        pattern = r'(\$\$.*?\$\$|\$.*?\$)'
        parts = re.split(pattern, text, flags=re.DOTALL)
        result = []

        for idx, part in enumerate(parts):
            if not part:
                continue

            if part.startswith('$$') and part.endswith('$$'):
                formula = part[2:-2].strip()
                try:
                    img_data = self._render_latex(formula, display_mode=True)
                    result.append({'type': 'image', 'data': img_data, 'index': idx})
                except Exception as e:
                    logger.error(f"渲染行间公式失败: {e}")
                    result.append({'type': 'text', 'text': part, 'index': idx})
            elif part.startswith('$') and part.endswith('$'):
                formula = part[1:-1].strip()
                try:
                    img_data = self._render_latex(formula, display_mode=False)
                    result.append({'type': 'image', 'data': img_data, 'index': idx})
                except Exception as e:
                    logger.error(f"渲染行内公式失败: {e}")
                    result.append({'type': 'text', 'text': part, 'index': idx})
            else:
                if part.strip():
                    result.append({'type': 'text', 'text': part, 'index': idx})

        result.sort(key=lambda x: x['index'])
        return result

    async def _send_with_latex(self, event: AstrMessageEvent, text: str):
        try:
            segments = self._process_latex(text)
            chain = []

            for seg in segments:
                if seg['type'] == 'text':
                    chain.append(Plain(seg['text']))
                else:
                    img_hash = hashlib.md5(seg['data']).hexdigest()[:12]
                    img_path = self.img_dir / f"latex_{img_hash}.png"

                    if not img_path.exists():
                        with open(img_path, 'wb') as f:
                            f.write(seg['data'])
                    chain.append(Image.fromFileSystem(str(img_path)))

            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.error(f"发送 LaTeX 消息失败: {e}")
            await event.send(event.plain_result(text))

    async def terminate(self):
        try:
            if self.img_dir.exists():
                for f in self.img_dir.glob("*.png"):
                    try:
                        f.unlink()
                    except Exception as e:
                        logger.debug(f"删除图片失败: {e}")
                try:
                    self.img_dir.rmdir()
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"清理资源时出错: {e}")
        logger.info("ComplexSolverPlugin 已卸载")