"""
AstrBot 复杂问题求解助手插件
通过强弱模型协作，自动分流复杂问题，人设化复述解答，支持多模态和LaTeX渲染。
"""

import re
import io
import hashlib
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
    desc="多模型协作求解插件，自动识别复杂问题并调用专业模型，人设化复述解题过程",
    version="2.0.11",
    repo="https://github.com/CYun2024"
)
class ComplexSolverPlugin(Star):
    """复杂问题求解助手"""

    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context
        
        config = config or {}
        
        self.solver_provider = config.get("solver_provider", "")
        self.solver_model = config.get("solver_model", "")
        self.enable_latex = config.get("enable_latex_render", True)
        self.enable_context = config.get("enable_context", True)
        self.context_timeout = config.get("context_timeout", 600)

        self.img_dir = Path(get_astrbot_data_path()) / "plugin_data" / "complex_solver" / "images"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ComplexSolverPlugin 已加载，配置: {config}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def handle_message(self, event: AstrMessageEvent):
        message = event.message_str.strip()
        if not message:
            return

        images = self._extract_images(event)
        umo = event.unified_msg_origin

        main_provider_id = await self.context.get_current_chat_provider_id(umo)
        if not main_provider_id:
            logger.warning("无法获取当前会话的主模型ID")
            return

        try:
            is_complex = await self._is_complex_question(main_provider_id, message, images)
        except Exception as e:
            logger.error(f"判断复杂问题时出错: {e}")
            return

        if not is_complex and self.enable_context:
            try:
                is_followup = await self._is_followup_question(main_provider_id, umo, message)
                if is_followup:
                    is_complex = True
            except Exception as e:
                logger.error(f"判断追问时出错: {e}")

        if not is_complex:
            return

        solver_provider_id = await self._get_solver_provider_id(event)
        if not solver_provider_id:
            apology = await self._generate_apology(main_provider_id, "解题服务暂时不可用，请稍后再试。", message)
            await event.send(event.plain_result(apology))
            event.stop_event()
            return

        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        history_messages = []
        if curr_cid:
            try:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation:
                    history_messages = conversation.history or []
            except Exception as e:
                logger.warning(f"获取历史对话失败: {e}")

        try:
            solver_answer = await self._call_solver(solver_provider_id, message, images, history_messages)
        except Exception as e:
            logger.error(f"调用解题模型时出错: {e}")
            solver_answer = None

        if solver_answer is None:
            apology = await self._generate_apology(main_provider_id, "解题助手暂时无法解答这个问题，请稍后重试。", message)
            await event.send(event.plain_result(apology))
            event.stop_event()
            return

        try:
            restated = await self._persona_restate(main_provider_id, solver_answer, message)
        except Exception as e:
            logger.error(f"人设复述失败: {e}，直接返回原答案")
            restated = solver_answer

        try:
            if self.enable_latex:
                await self._send_with_latex(event, restated)
            else:
                await event.send(event.plain_result(restated))
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            await event.send(event.plain_result(restated))

        if curr_cid and self.enable_context:
            try:
                from astrbot.core.agent.message import UserMessageSegment, AssistantMessageSegment, TextPart
                user_msg = UserMessageSegment(content=[TextPart(text=message)])
                assistant_msg = AssistantMessageSegment(content=[TextPart(text=solver_answer)])
                await conv_mgr.add_message_pair(curr_cid, user_msg, assistant_msg)
            except Exception as e:
                logger.warning(f"保存对话历史失败: {e}")

        event.stop_event()

    # ---------- 私有辅助方法 ----------
    def _extract_images(self, event: AstrMessageEvent) -> List[str]:
        try:
            return event.get_images()
        except AttributeError:
            return []

    async def _is_complex_question(self, provider_id: str, question: str, images: List[str]) -> bool:
        prompt = """请判断以下用户问题是否属于复杂问题，需要调用强大的专业模型来解决。
复杂问题包括但不限于：数学计算、逻辑推理、代码编写、专业学科问答、需要多步推理的问题。
如果问题是日常闲聊、简单问候、情感表达等，则不属于复杂问题。

用户问题：{question}

请只输出"COMPLEX"或"SIMPLE"，不要输出其他内容。""".format(question=question)
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=images
            )
            return resp.completion_text.strip().upper() == "COMPLEX"
        except Exception as e:
            logger.error(f"复杂问题判断失败: {e}")
            return False

    async def _is_followup_question(self, provider_id: str, umo: str, question: str) -> bool:
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

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            return resp.completion_text.strip().upper() == "FOLLOWUP"
        except Exception as e:
            logger.error(f"追问判断失败: {e}")
            return False

    async def _get_solver_provider_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取解题提供商ID：直接返回配置值，如果未配置则尝试回退到当前会话主模型"""
        if not self.solver_provider:
            logger.warning("未配置解题模型提供商，尝试使用当前会话主模型作为备选")
            main_provider = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            if main_provider:
                logger.info(f"使用当前会话主模型提供商: {main_provider}")
                return main_provider
            else:
                logger.error("无法获取当前会话主模型提供商")
                return None
        logger.info(f"使用解题提供商: {self.solver_provider}")
        return self.solver_provider

    async def _call_solver(self, provider_id: str, question: str, images: List[str], history_messages: List) -> Optional[str]:
        messages = []
        
        if history_messages and self.enable_context:
            for msg in history_messages[-4:]:  # 只取最近4条
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
        full_prompt = "\n".join(messages)

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=full_prompt,
                image_urls=images
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"解题模型调用失败: {e}")
            return None

    async def _persona_restate(self, provider_id: str, text: str, original_question: str = "") -> str:
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            if isinstance(persona, dict):
                persona_prompt = persona.get('prompt', "一个乐于助人的助手")
            else:
                persona_prompt = getattr(persona, 'prompt', "一个乐于助人的助手") if persona else "一个乐于助人的助手"
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}，使用默认")
            persona_prompt = "一个乐于助人的助手"

        prompt = f"""你是一个{persona_prompt}。用户提出了一个复杂问题，现在你需要用你的角色风格重新表述下面专业模型给出的完整解答过程。要求：
1. 必须严格保留解答的逻辑、步骤和正确性，不得修改任何数学公式、推理步骤。
2. 用你的角色口吻重新组织语言，可以添加符合角色设定的语气词、表情符号等。
3. 如果解答中包含LaTeX数学公式，请保留原样（例如$...$或$$...$$），不要修改。

用户问题：{original_question}

专业模型解答：
{text}

请用你的角色风格重新表述以上解答："""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"人设复述失败: {e}")
            return text

    async def _generate_apology(self, provider_id: str, reason: str, original_question: str) -> str:
        """生成符合人设的道歉消息"""
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

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"生成道歉消息失败: {e}")
            return f"抱歉，{reason}"

    def _render_latex(self, formula: str, display_mode: bool = False) -> bytes:
        """渲染 LaTeX 公式为图片"""
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
        """处理文本中的 LaTeX 公式"""
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
        """发送包含 LaTeX 公式的消息"""
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
        """插件卸载时清理资源"""
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