"""
问题复杂度判断模块
判断问题是否为复杂问题，以及是否为追问
"""
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter


class QuestionJudge:
    """问题判断器"""
    
    def __init__(self, context: Context, debugger: DebuggerReporter):
        self.context = context
        self.debugger = debugger
    
    async def is_complex_question(
        self,
        provider_id: str,
        question: str,
        images: List[str],
        sender_info: dict,
        conv_id: str
    ) -> bool:
        """判断是否为复杂问题"""
        prompt = f"""请判断以下用户问题是否属于复杂问题，需要调用强大的专业模型来解决。
复杂问题包括但不限于：数学计算、逻辑推理、代码编写、专业学科问答、需要多步推理的问题。
如果问题是日常闲聊、简单问候、情感表达等，则不属于复杂问题。

用户问题：{question}

请只输出"COMPLEX"或"SIMPLE"，不要输出其他内容。"""
        
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=images
            )
            result = resp.completion_text.strip().upper() == "COMPLEX"
            
            # 上报到debugger
            await self.debugger.report_complexity_judge(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                question=question,
                images=images,
                result=result,
                sender_info=sender_info,
                conv_id=conv_id
            )
            
            return result
        except Exception as e:
            logger.error(f"复杂问题判断失败: {e}")
            return False
    
    async def is_followup_question(
        self,
        provider_id: str,
        conversation_history: List,
        question: str,
        sender_info: dict,
        conv_id: str
    ) -> bool:
        """判断是否为追问"""
        if not conversation_history:
            return False
        
        last_user = last_assistant = None
        
        for msg in reversed(conversation_history):
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
            result = resp.completion_text.strip().upper() == "FOLLOWUP"
            
            # 上报到debugger
            await self.debugger.report_followup_judge(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                last_user=last_user,
                last_assistant=last_assistant,
                question=question,
                result=result,
                sender_info=sender_info,
                conv_id=conv_id
            )
            
            return result
        except Exception as e:
            logger.error(f"追问判断失败: {e}")
            return False
