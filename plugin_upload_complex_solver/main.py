"""
AstrBot 复杂问题求解助手插件
通过强弱模型协作，自动分流复杂问题，人设化复述解答，支持多模态和LaTeX渲染。
支持双模型并行调用、超时重试机制、独立精简模型、@提及功能。

版本: 3.1.0
作者: CYun2024
"""

from pathlib import Path
from typing import Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 导入子模块
from .debugger_reporter import DebuggerReporter
from .at_handler import AtHandler
from .latex_renderer import LatexRenderer
from .persona import PersonaHandler
from .summarizer import Summarizer
from .solver import Solver
from .question_judge import QuestionJudge
from .utils import extract_images


@register(
    name="complex_solver",
    author="CYun2024",
    desc="多模型协作求解插件，自动识别复杂问题并调用专业模型，支持@提及和双模型并行",
    version="3.1.0",
    repo="https://github.com/CYun2024"
)
class ComplexSolverPlugin(Star):
    """复杂问题求解助手 - 支持@提及和双模型并行版"""

    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context
        
        # 加载配置
        config = config or {}
        
        # 解题模型配置
        self.solver_provider = config.get("solver_provider", "")
        self.solver_model = config.get("solver_model", "")
        self.solver_provider_2 = config.get("solver_provider_2", "")
        self.solver_model_2 = config.get("solver_model_2", "")
        
        # 精简模型配置
        self.summarize_provider = config.get("summarize_provider", "")
        self.summarize_model = config.get("summarize_model", "")
        
        # 功能开关配置
        self.enable_latex = config.get("enable_latex_render", True)
        self.enable_context = config.get("enable_context", True)
        self.enable_summarize = config.get("enable_summarize", True)
        self.enable_persona_restate = config.get("enable_persona_restate", True)
        self.enable_at_mention = config.get("enable_at_mention", True)
        
        # 其他配置
        self.context_timeout = config.get("context_timeout", 600)
        self.max_wait_minutes = config.get("max_wait_minutes", 9)
        
        # 图片目录
        self.img_dir = Path(get_astrbot_data_path()) / "plugin_data" / "complex_solver" / "images"
        
        # 初始化各模块
        self._init_modules()
        
        mode_str = "双模型" if self.solver_provider_2 else "单模型"
        persona_str = "人设复述" if self.enable_persona_restate else "精简模型直接用人设输出"
        at_str = "开启@提及" if self.enable_at_mention else "关闭@提及"
        logger.info(f"ComplexSolverPlugin v3.1 已加载，{mode_str}模式，{persona_str}，{at_str}")
    
    def _init_modules(self):
        """初始化各功能模块"""
        # Debugger上报器
        self.debugger = DebuggerReporter(self.context)
        
        # @提及处理器
        self.at_handler = AtHandler()
        
        # LaTeX渲染器
        self.latex_renderer = LatexRenderer(self.img_dir)
        
        # 人设处理器
        self.persona_handler = PersonaHandler(self.context, self.debugger)
        
        # 精简处理器
        self.summarizer = Summarizer(
            self.context,
            self.debugger,
            self.summarize_provider,
            self.summarize_model,
            self.solver_provider,
            self.solver_model
        )
        
        # 解题处理器
        self.solver = Solver(
            self.context,
            self.debugger,
            self.solver_provider,
            self.solver_model,
            self.solver_provider_2,
            self.solver_model_2,
            self.max_wait_minutes,
            self.enable_context
        )
        
        # 问题判断器
        self.question_judge = QuestionJudge(self.context, self.debugger)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def handle_message(self, event: AstrMessageEvent):
        """主消息处理流程"""
        message = event.message_str.strip()
        if not message:
            return
        
        # 提取信息
        images = extract_images(event)
        umo = event.unified_msg_origin
        sender_info = self.at_handler.extract_sender_info(event)
        
        # 获取会话ID
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        
        # 获取主模型ID
        main_provider_id = await self.context.get_current_chat_provider_id(umo)
        if not main_provider_id:
            logger.warning("无法获取当前会话的主模型ID")
            return
        
        # 判断是否为复杂问题
        try:
            is_complex = await self.question_judge.is_complex_question(
                main_provider_id, message, images, sender_info, curr_cid
            )
        except Exception as e:
            logger.error(f"判断复杂问题时出错: {e}")
            return
        
        # 判断是否为追问
        if not is_complex and self.enable_context:
            try:
                history = []
                if curr_cid:
                    conversation = await conv_mgr.get_conversation(umo, curr_cid)
                    if conversation:
                        history = conversation.history or []
                
                is_followup = await self.question_judge.is_followup_question(
                    main_provider_id, history, message, sender_info, curr_cid
                )
                if is_followup:
                    is_complex = True
            except Exception as e:
                logger.error(f"判断追问时出错: {e}")
        
        # 不是复杂问题，不处理
        if not is_complex:
            return
        
        # 检查是否有配置解题模型
        if not self.solver_provider and not self.solver_provider_2:
            apology = await self.persona_handler.generate_apology(
                main_provider_id,
                "解题服务未配置，请管理员配置解题模型",
                message,
                sender_info,
                curr_cid
            )
            await self._send_result(event, apology, sender_info)
            event.stop_event()
            return
        
        # 获取历史消息
        history_messages = []
        if curr_cid:
            try:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation:
                    history_messages = conversation.history or []
            except Exception as e:
                logger.warning(f"获取历史对话失败: {e}")
        
        # 调用解题模型（带重试机制）
        async def send_waiting_message(round_num: int):
            """发送等待消息回调"""
            waiting_msg = await self.persona_handler.generate_waiting_message(
                main_provider_id, round_num, message, sender_info, curr_cid
            )
            await event.send(event.plain_result(waiting_msg))
        
        solver_answer, success = await self.solver.solve_with_retry(
            message, images, history_messages, sender_info, curr_cid,
            waiting_callback=send_waiting_message if self.enable_context else None
        )
        
        if not success or not solver_answer:
            apology = await self.persona_handler.generate_apology(
                main_provider_id,
                f"解题助手在 {self.max_wait_minutes} 分钟内未能获取有效解答，请稍后重试或简化问题",
                message,
                sender_info,
                curr_cid
            )
            await self._send_result(event, apology, sender_info)
            event.stop_event()
            return
        
        # 处理输出
        final_answer = await self._process_output(
            message, solver_answer, sender_info, curr_cid, main_provider_id
        )
        
        # 发送结果
        await self._send_result(event, final_answer, sender_info)
        
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
    
    async def _process_output(
        self,
        original_question: str,
        solver_answer: str,
        sender_info: dict,
        conv_id: str,
        main_provider_id: str
    ) -> str:
        """
        处理输出流程
        根据配置决定是用人设复述还是让精简模型直接用人设输出
        """
        result = solver_answer
        
        # 步骤1: 精简（如果需要）
        if self.enable_summarize:
            try:
                persona_prompt = ""
                if not self.enable_persona_restate:
                    # 如果不启用主LLM复述，需要获取人设给精简模型
                    persona_prompt = await self.persona_handler.get_persona_prompt()
                
                summarized = await self.summarizer.summarize(
                    original_question,
                    solver_answer,
                    sender_info,
                    conv_id,
                    persona_prompt=persona_prompt,
                    use_persona=not self.enable_persona_restate
                )
                if summarized:
                    result = summarized
                    logger.info("解题输出精简成功")
            except Exception as e:
                logger.error(f"精简解题输出时出错: {e}，使用原答案")
        
        # 步骤2: 人设复述（仅当启用时）
        if self.enable_persona_restate:
            try:
                restated = await self.persona_handler.restate_with_persona(
                    main_provider_id, result, original_question, sender_info, conv_id
                )
                result = restated
            except Exception as e:
                logger.error(f"人设复述失败: {e}，使用原答案")
        
        return result
    
    async def _send_result(self, event: AstrMessageEvent, text: str, sender_info: dict):
        """发送结果，处理@提及和LaTeX渲染"""
        # 处理@提及
        if self.enable_at_mention and sender_info.get("group_id"):
            # 检查是否已有@标签
            if not self.at_handler.valid_at_pattern.search(text):
                # 添加@提问者
                text = self.at_handler.add_at_to_text(
                    text,
                    sender_info["id"],
                    sender_info["name"],
                    bool(sender_info.get("group_id"))
                )
        
        # 发送消息
        try:
            if self.enable_latex:
                # 处理@标签和LaTeX
                if self.at_handler.valid_at_pattern.search(text):
                    # 有@标签，需要特殊处理
                    components = self.at_handler.process_at_tags(text)
                    # 检查是否有LaTeX
                    has_latex = '$' in text
                    if has_latex and len(components) == 1 and isinstance(components[0], Plain):
                        # 纯文本但有LaTeX，使用LaTeX渲染器
                        await self.latex_renderer.send_with_latex(event, text)
                    else:
                        # 有@组件或其他，直接发送
                        await event.send(event.chain_result(components))
                else:
                    # 没有@标签，直接使用LaTeX渲染器
                    await self.latex_renderer.send_with_latex(event, text)
            else:
                # 不渲染LaTeX，直接发送
                if self.enable_at_mention:
                    components = self.at_handler.process_at_tags(text)
                    await event.send(event.chain_result(components))
                else:
                    await event.send(event.plain_result(text))
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            await event.send(event.plain_result(text))

    async def terminate(self):
        """插件卸载时的清理"""
        try:
            self.latex_renderer.cleanup()
        except Exception as e:
            logger.error(f"清理资源时出错: {e}")
        logger.info("ComplexSolverPlugin 已卸载")
