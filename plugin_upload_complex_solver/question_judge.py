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
        logger.info(f"[QuestionJudge] 开始判断问题复杂度")
        logger.info(f"[QuestionJudge] 问题内容: {question[:100]}...")
        logger.debug(f"[QuestionJudge] 图片数量: {len(images)}")
        
        # 检查是否包含图片描述（用户可能发了图片）
        has_image_description = "[图片内容描述：" in question
        logger.info(f"[QuestionJudge] 是否包含图片描述: {has_image_description}")
        
        # 【关键】如果包含图片描述，强制判定为复杂问题（提高优先级）
        if has_image_description:
            logger.info(f"[QuestionJudge] 检测到图片描述，直接进入复杂问题处理流程")
            # 仍然上报到 debugger 但不实际调用模型判断，节省资源
            await self.debugger.report_complexity_judge(
                provider_id=provider_id,
                model="forced_by_image",
                question=question,
                images=images,
                result=True,
                sender_info=sender_info,
                conv_id=conv_id
            )
            return True
        
        # 检查明显的视觉/识别类关键词
        visual_keywords = ['图片', '图', '照片', '截图', '看看', '写的是什么', '写的什么', 
                          '内容', '写了', '上面写着', '图中', '这张图', '这个图']
        has_visual_keyword = any(kw in question for kw in visual_keywords)
        
        if has_visual_keyword and images:
            logger.info(f"[QuestionJudge] 检测到视觉类关键词 + 图片，判定为复杂问题")
            await self.debugger.report_complexity_judge(
                provider_id=provider_id,
                model="forced_by_visual_keyword",
                question=question,
                images=images,
                result=True,
                sender_info=sender_info,
                conv_id=conv_id
            )
            return True
        
        prompt = f"""请严格判断以下用户问题是否属于**必须由专业AI模型解决的复杂学术/技术问题**。

复杂问题包括：数学计算、物理/化学/生物等学科问题、逻辑推理题、代码编写、算法设计、需要多步推理的学术问题、图片内容识别与分析。

**不属于复杂问题的情况**（必须判定为SIMPLE）：
- 日常闲聊、问候、情感表达
- 简单的事实查询（如"今天星期几"、"什么是XX"）
- 讨论性、观点性问题（如"你觉得XX怎么样"）
- 其他群成员的对话讨论，用户只是在参与聊天
- 简短随机的语句

**特别重要**：
- 如果用户询问图片内容（如"这张图写了什么"、"看看这个图片"），必须判定为COMPLEX
- 如果消息包含"[图片内容描述：...]"，说明用户发送了图片且需要分析，必须判定为COMPLEX
- 如果问题包含"写"、"求解"、"计算"、"证明"、"为什么"、"怎么做"等学术动词，倾向于COMPLEX

用户问题：{question}

请只输出"COMPLEX"或"SIMPLE"，不要输出其他内容。"""
        
        logger.debug(f"[QuestionJudge] 发送给判断模型的Prompt:\n{prompt}")
        
        try:
            logger.info(f"[QuestionJudge] 调用判断模型: {provider_id}")
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=images
            )
            result_text = resp.completion_text.strip().upper()
            result = result_text == "COMPLEX"
            
            # 记录判断依据到日志
            if result:
                logger.info(f"[QuestionJudge] 判定结果: COMPLEX (复杂问题)")
            else:
                logger.info(f"[QuestionJudge] 判定结果: SIMPLE (简单问题)")
            logger.debug(f"[QuestionJudge] 模型原始输出: {result_text}")
            
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
            logger.error(f"[QuestionJudge] 复杂问题判断失败: {e}")
            import traceback
            logger.debug(f"[QuestionJudge] 异常堆栈: {traceback.format_exc()}")
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
            logger.debug(f"[QuestionJudge] 无历史记录，不是追问")
            return False
        
        logger.info(f"[QuestionJudge] 开始判断是否为追问，历史消息数: {len(conversation_history)}")
        
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
                    logger.debug(f"[QuestionJudge] 找到上一条用户消息: {last_user[:50]}...")
                elif role == 'assistant' and last_assistant is None:
                    last_assistant = str(content) if content else ""
                    logger.debug(f"[QuestionJudge] 找到上一条助手回复: {last_assistant[:50]}...")
                
                if last_user and last_assistant:
                    break
                    
            except Exception as e:
                logger.debug(f"[QuestionJudge] 解析历史消息时出错: {e}")
                continue
        
        if not last_user or not last_assistant:
            logger.debug(f"[QuestionJudge] 未找到完整对话上下文，不是追问")
            return False
        
        prompt = f"""请判断以下新消息是否是对之前问题的**直接追问**（例如请求解释某一步、询问原因、要求补充细节、追问答案等）。

之前用户问题：{last_user[:200]}
之前解答：{last_assistant[:200]}

新消息：{question}

**判断标准**：
- FOLLOWUP: 用户明确在追问之前的答案（如"为什么"、"怎么来的"、"详细说说"、"不懂"等）
- NEW: 用户提出了新的话题或问题，与之前无关

**注意**：如果只是随意的回应（如"好的"、"谢谢"、"哈哈"），也判定为NEW。

请只输出"FOLLOWUP"或"NEW"，不要输出其他内容。"""

        try:
            logger.info(f"[QuestionJudge] 调用模型判断追问...")
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result_text = resp.completion_text.strip().upper()
            result = result_text == "FOLLOWUP"
            
            if result:
                logger.info(f"[QuestionJudge] 判定为追问: {question[:50]}...")
            else:
                logger.debug(f"[QuestionJudge] 判定为新问题: {question[:50]}...")
            
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
            logger.error(f"[QuestionJudge] 追问判断失败: {e}")
            return False