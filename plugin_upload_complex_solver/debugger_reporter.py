"""
LLM Debugger 上报模块
修复接口对齐问题，确保能正确上报到 LLM Debugger
"""
from datetime import datetime
from typing import Dict, Any, Optional, List

from astrbot.api import logger
from astrbot.api.star import Context

from .utils import make_serializable, truncate_text


class DebuggerReporter:
    """LLM Debugger 上报器"""
    
    def __init__(self, context: Context):
        self.context = context
        self._debugger_instance = None
        self._debugger_checked = False
    
    def _get_debugger(self):
        """获取 LLM Debugger 实例（带缓存）"""
        if self._debugger_checked:
            return self._debugger_instance
        
        self._debugger_checked = True
        try:
            # 尝试通过多种方式获取 debugger
            debugger = None
            
            # 方式1: 通过 get_registered_star
            try:
                debugger = self.context.get_registered_star("llm_debugger")
            except Exception:
                pass
            
            # 方式2: 通过 provider 或 star 列表查找
            if not debugger:
                try:
                    stars = getattr(self.context, '_stars', {}) or {}
                    for name, star in stars.items():
                        if 'debugger' in name.lower() or 'llm_debug' in name.lower():
                            debugger = star
                            break
                except Exception:
                    pass
            
            # 方式3: 通过 context 的 star_map 查找
            if not debugger:
                try:
                    star_map = getattr(self.context, 'star_map', {}) or {}
                    for name, star in star_map.items():
                        if 'debugger' in name.lower() or 'llm_debug' in name.lower():
                            debugger = star
                            break
                except Exception:
                    pass
            
            if debugger and hasattr(debugger, 'record_llm_call'):
                self._debugger_instance = debugger
                logger.info(f"[DebuggerReporter] 成功连接到 LLM Debugger")
                return debugger
            else:
                logger.debug(f"[DebuggerReporter] 未找到 LLM Debugger 或缺少 record_llm_call 方法")
                return None
                
        except Exception as e:
            logger.debug(f"[DebuggerReporter] 获取 LLM Debugger 失败: {e}")
            return None
    
    async def report_request(
        self,
        provider_id: str,
        model: str,
        prompt: str,
        images: List[str],
        purpose: str,
        sender_info: dict,
        conv_id: str,
        system_prompt: str = "",
        contexts: List = None
    ):
        """上报 LLM 请求"""
        debugger = self._get_debugger()
        if not debugger:
            return
        
        try:
            data = {
                "phase": "request",
                "provider_id": provider_id,
                "model": model or "unknown",
                "prompt": prompt,
                "images": images or [],
                "source": {"plugin": "complex_solver", "purpose": purpose},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat(),
                "system_prompt": system_prompt,
                "contexts": contexts or []
            }
            await debugger.record_llm_call(data)
            logger.debug(f"[DebuggerReporter] 已上报请求: {purpose}")
        except Exception as e:
            logger.debug(f"[DebuggerReporter] 上报请求失败: {e}")
    
    async def report_response(
        self,
        provider_id: str,
        model: str,
        response: str,
        purpose: str,
        sender_info: dict,
        conv_id: str,
        usage: Any = None
    ):
        """上报 LLM 响应"""
        debugger = self._get_debugger()
        if not debugger:
            return
        
        try:
            data = {
                "phase": "response",
                "provider_id": provider_id,
                "model": model or "unknown",
                "response": response,
                "source": {"plugin": "complex_solver", "purpose": purpose},
                "sender": sender_info,
                "conversation_id": conv_id,
                "timestamp": datetime.now().isoformat(),
                "usage": make_serializable(usage) if usage else None
            }
            await debugger.record_llm_call(data)
            logger.debug(f"[DebuggerReporter] 已上报响应: {purpose}")
        except Exception as e:
            logger.debug(f"[DebuggerReporter] 上报响应失败: {e}")
    
    async def report_complexity_judge(
        self,
        provider_id: str,
        model: str,
        question: str,
        images: List[str],
        result: bool,
        sender_info: dict,
        conv_id: str
    ):
        """上报复杂问题判断"""
        prompt = f"""请判断以下用户问题是否属于复杂问题，需要调用强大的专业模型来解决。
复杂问题包括但不限于：数学计算、逻辑推理、代码编写、专业学科问答、需要多步推理的问题。
如果问题是日常闲聊、简单问候、情感表达等，则不属于复杂问题。

用户问题：{question}

请只输出"COMPLEX"或"SIMPLE"，不要输出其他内容。"""
        
        await self.report_request(
            provider_id=provider_id,
            model=model,
            prompt=prompt,
            images=images,
            purpose="complexity_judge",
            sender_info=sender_info,
            conv_id=conv_id
        )
        
        await self.report_response(
            provider_id=provider_id,
            model=model,
            response="COMPLEX" if result else "SIMPLE",
            purpose="complexity_judge",
            sender_info=sender_info,
            conv_id=conv_id
        )
    
    async def report_followup_judge(
        self,
        provider_id: str,
        model: str,
        last_user: str,
        last_assistant: str,
        question: str,
        result: bool,
        sender_info: dict,
        conv_id: str
    ):
        """上报追问判断"""
        prompt = f"""以下是之前用户提出的复杂问题和专业模型的解答。
现在用户又发送了一条新消息。请判断这条新消息是否是对之前问题的追问
（例如请求解释某一步、询问原因等）。如果是，输出"FOLLOWUP"，否则输出"NEW"。

之前用户问题：{last_user}
之前解答：{last_assistant}

新消息：{question}

请只输出"FOLLOWUP"或"NEW"。"""
        
        await self.report_request(
            provider_id=provider_id,
            model=model,
            prompt=prompt,
            images=[],
            purpose="followup_judge",
            sender_info=sender_info,
            conv_id=conv_id
        )
        
        await self.report_response(
            provider_id=provider_id,
            model=model,
            response="FOLLOWUP" if result else "NEW",
            purpose="followup_judge",
            sender_info=sender_info,
            conv_id=conv_id
        )
