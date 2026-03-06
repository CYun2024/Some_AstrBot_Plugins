"""
AstrBot 复杂问题求解助手插件
"""

from pathlib import Path
from typing import Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, At
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 导入子模块
from .debugger_reporter import DebuggerReporter
from .at_handler import AtHandler
from .latex_renderer import LatexRenderer
from .persona import PersonaHandler
from .summarizer import Summarizer
from .solver import Solver
from .intent_judge import IntentJudge
from .utils import extract_images
from .vision_handler import VisionHandler


@register(
    name="complex_solver",
    author="CYun2024",
    desc="双视觉模型备份，精简模型统一输出",
    version="4.2.0",
    repo="https://github.com/CYun2024"
)
class ComplexSolverPlugin(Star):
    """双视觉模型精简版求解器"""

    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context
        config = config or {}

        # === 模型配置（删除model字段）===
        self.judge_provider = config.get("judge_provider", "")

        self.vision_ocr_provider = config.get("vision_ocr_provider", "")
        self.vision_scene_provider_1 = config.get("vision_scene_provider_1", "")
        self.vision_scene_provider_2 = config.get("vision_scene_provider_2", "")

        self.solver_provider = config.get("solver_provider", "")
        self.solver_provider_2 = config.get("solver_provider_2", "")
        self.summarize_provider = config.get("summarize_provider", "")

        # === 功能开关 ===
        self.enable_latex = config.get("enable_latex_render", True)
        self.enable_context = config.get("enable_context", True)
        self.enable_persona_restate = config.get("enable_persona_restate", True)
        self.enable_at_mention = config.get("enable_at_mention", True)
        self.enable_whole_render = config.get("enable_whole_render", False)
        
        self.strict_trigger = config.get("strict_trigger", True)
        self.bot_names = config.get("bot_names", ["韶梦", "shaomeng"])
        self.debug_mode = config.get("debug_mode", False)

        # 其他配置
        self.context_timeout = config.get("context_timeout", 600)
        self.max_wait_minutes = config.get("max_wait_minutes", 9)
        self.vision_timeout = 90  # 固定90秒

        # 图片目录
        self.img_dir = Path(get_astrbot_data_path()) / "plugin_data" / "complex_solver" / "images"

        self._init_modules()
        
        logger.info(f"[ComplexSolver] v4.1.1 已加载")
        logger.info(f"[ComplexSolver] OCR专用: {self.vision_ocr_provider or '未配置'}")
        logger.info(f"[ComplexSolver] 多模态主模型: {self.vision_scene_provider_1 or '未配置'}")
        logger.info(f"[ComplexSolver] 多模态副模型: {self.vision_scene_provider_2 or '未配置'}")

    def _init_modules(self):
        """初始化模块"""
        self.debugger = DebuggerReporter(self.context)
        self.at_handler = AtHandler()
        self.latex_renderer = LatexRenderer(self.img_dir)
        self.persona_handler = PersonaHandler(self.context, self.debugger)
        
        self.intent_judge = IntentJudge(
            self.context, 
            self.debugger,
            self.judge_provider,
            ""  # 不再传model
        )
        
        self.vision_handler = VisionHandler(
            self.context,
            self.debugger,
            self.vision_ocr_provider,      # OCR专用
            self.vision_scene_provider_1,  # 多模态主
            self.vision_scene_provider_2,  # 多模态备
            self.vision_timeout
        )
        
        self.solver = Solver(
            self.context,
            self.debugger,
            self.solver_provider,
            "",  # 不再传model
            self.solver_provider_2,
            "",  # 不再传model
            self.max_wait_minutes,
            self.enable_context
        )
        
        self.summarizer = Summarizer(
            self.context,
            self.debugger,
            self.summarize_provider,
            self.solver_provider
        )

    def _should_process_message(self, event: AstrMessageEvent, message: str) -> bool:
        """判断是否应处理 - 更严格的触发检查"""
        try:
            group_id = event.get_group_id()
            is_private = not group_id or str(group_id) == "None"
        except:
            is_private = True
        
        # 私聊总是处理
        if is_private:
            return True
        
        message_lower = message.lower()
        
        # 检查是否@Bot
        if event.message_obj and hasattr(event.message_obj, 'message'):
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    # 检查是否是@自己
                    try:
                        if str(comp.qq) == str(self.context.get_self_id()):
                            return True
                    except:
                        pass
        
        # 检查是否包含Bot名字
        for name in self.bot_names:
            if name.lower() in message_lower:
                return True
        
        return False

    def _is_explicit_vision_request(self, message: str) -> bool:
        """检查是否是明确的识图请求"""
        message_lower = message.lower()
        
        # 明确的识图关键词
        explicit_keywords = [
            "看图", "看图片", "识图", "识别图片", "这张图片", "这张图",
            "图片里", "图片上", "图中", "图里", "照片里", "截图",
            "写了什么", "文字", "提取文字", "ocr", "识别文字",
            "这道题", "这道题怎么", "题目", "问题是什么"
        ]
        
        for kw in explicit_keywords:
            if kw in message_lower:
                return True
        
        return False

    async def _insert_context_silently(self, event: AstrMessageEvent, user_text: str, assistant_text: str, conv_id: str):
        """
        静默插入上下文 - 加入历史但不触发输出
        使用 AstrBot 的 conversation_manager
        """
        if not conv_id or not self.enable_context:
            return
        
        try:
            from astrbot.core.agent.message import UserMessageSegment, AssistantMessageSegment, TextPart
            
            # 构建消息段
            user_msg = UserMessageSegment(content=[TextPart(text=user_text)])
            assistant_msg = AssistantMessageSegment(content=[TextPart(text=assistant_text)])
            
            # 添加到对话历史
            await self.context.conversation_manager.add_message_pair(
                conv_id, 
                user_msg, 
                assistant_msg
            )
            
            logger.debug(f"[ComplexSolver] 已静默插入上下文: 用户'{user_text[:30]}...' -> 助手'{assistant_text[:30]}...'")
            
        except Exception as e:
            logger.warning(f"[ComplexSolver] 插入上下文失败: {e}")

    def _message_has_bot_name(self, message: str) -> bool:
        """检查消息中是否已包含Bot名字"""
        message_lower = message.lower()
        return any(name.lower() in message_lower for name in self.bot_names)


    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def handle_message(self, event: AstrMessageEvent):
        """主消息处理流程 - 严格触发控制"""
        message = event.message_str.strip()
        if not message:
            return
        
        # 【防死循环】检查是否已被处理过
        if event.get_extra("_complex_solver_processed"):
            logger.debug("[ComplexSolver] 消息已处理过，跳过")
            return
        
        # === 严格触发检查（最前置）===
        if self.strict_trigger:
            if not self._should_process_message(event, message):
                logger.debug(f"[ComplexSolver] 未触发严格模式，跳过处理")
                return
        
        logger.info(f"[ComplexSolver] ===== 开始处理消息 =====")
        logger.info(f"[ComplexSolver] 消息内容: {message[:80]}...")
        logger.info(f"[ComplexSolver] Bot名字列表: {self.bot_names}")
        
        # === 步骤1: 提取图片 ===
        images = extract_images(event)
        has_images = len(images) > 0
        logger.info(f"[ComplexSolver] 提取到 {len(images)} 张图片")
        
        # 【关键修复】如果有图片但不是明确的识图请求，不自动触发识图
        # 除非消息中包含Bot名字或@（上面已经检查过）
        if has_images and not self._is_explicit_vision_request(message):
            logger.info(f"[ComplexSolver] 虽有图片但无明确识图意图，跳过识图流程")
            has_images = False
            images = []
        
        # 获取基础信息
        umo = event.unified_msg_origin
        sender_info = self.at_handler.extract_sender_info(event)
        conv_mgr = self.context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        main_provider_id = await self.context.get_current_chat_provider_id(umo)
        
        # === 步骤2: 意图判断（仅在严格触发后执行）===
        vision_triggered = False
        vision_result = ""
        final_answer = ""
        is_complex = False
        
        if has_images:
            logger.info("[ComplexSolver] 有图片且触发条件满足，开始意图识别...")
            
            intent_data = await self.intent_judge.classify_intent(
                message, has_images, sender_info, curr_cid
            )
            
            intent = intent_data.get("intent", "chat")
            needs_vision = intent_data.get("needs_vision", False)
            
            logger.info(f"[ComplexSolver] 意图识别结果: intent={intent}, needs_vision={needs_vision}")
            
            # 处理OCR或解题意图：优先使用OCR专用模型
            if needs_vision and intent in ["ocr", "solver"]:
                logger.info(f"[ComplexSolver] 触发OCR/解题意图处理流程")
                
                if self.vision_handler.is_ocr_configured():
                    logger.info("[ComplexSolver] 步骤1: 调用OCR专用模型提取文字...")
                    ocr_result = await self.vision_handler.ocr_extract(
                        images, message, sender_info, curr_cid
                    )
                    
                    logger.info(f"[ComplexSolver] OCR专用模型返回长度: {len(ocr_result)}")
                    
                    # 检查OCR结果是否有效（非错误标记开头）
                    if ocr_result and not ocr_result.startswith("["):
                        # 检查是否为乱码
                        logger.info("[ComplexSolver] 检查OCR结果质量...")
                        garbled_check = await self.intent_judge.check_garbled_text(ocr_result)
                        
                        if not garbled_check.get("is_garbled", False):
                            logger.info("[ComplexSolver] OCR结果有效，直接使用")
                            vision_result = ocr_result
                            vision_triggered = True
                        else:
                            logger.warning(f"[ComplexSolver] OCR结果疑似乱码({garbled_check.get('reason', 'unknown')})，转用多模态模型兜底")
                            # OCR乱码，使用多模态场景模型兜底
                            if self.vision_handler.is_scene_configured():
                                vision_result = await self.vision_handler.scene_analyze(
                                    images, 
                                    "图片中的文字识别为乱码，请尽可能描述或猜测图片中的文字内容", 
                                    sender_info, 
                                    curr_cid
                                )
                                vision_triggered = True if (vision_result and not vision_result.startswith("[")) else False
                    else:
                        logger.warning(f"[ComplexSolver] OCR专用模型返回错误: {ocr_result[:100]}...")
                        # OCR失败，尝试多模态模型
                        if self.vision_handler.is_scene_configured():
                            logger.info("[ComplexSolver] 步骤2: OCR失败，尝试多模态主备模型...")
                            vision_result = await self.vision_handler.scene_analyze(
                                images, message, sender_info, curr_cid
                            )
                            vision_triggered = True if (vision_result and not vision_result.startswith("[")) else False
                else:
                    # 没有OCR专用模型，直接用多模态主备
                    logger.info("[ComplexSolver] OCR专用模型未配置，直接使用多模态主备模型...")
                    if self.vision_handler.is_scene_configured():
                        vision_result = await self.vision_handler.scene_analyze(
                            images, message, sender_info, curr_cid
                        )
                        vision_triggered = True if (vision_result and not vision_result.startswith("[")) else False
                        
            # 处理场景理解意图：直接使用多模态主备模型
            elif needs_vision and intent == "scene":
                logger.info("[ComplexSolver] 触发场景理解意图处理流程")
                if self.vision_handler.is_scene_configured():
                    vision_result = await self.vision_handler.scene_analyze(
                        images, message, sender_info, curr_cid
                    )
                    vision_triggered = True if (vision_result and not vision_result.startswith("[")) else False
                else:
                    logger.warning("[ComplexSolver] 多模态模型未配置，无法处理场景理解")
            
            # 如果成功触发了识图，经过精简模型处理
            if vision_triggered and vision_result:
                logger.info(f"[ComplexSolver] 视觉识别成功，准备调用精简模型精炼...")
                logger.debug(f"[ComplexSolver] 原始视觉结果前200字: {vision_result[:200]}...")
                
                # 获取人设（如果需要）
                persona_prompt = ""
                if self.enable_persona_restate:
                    try:
                        persona_prompt = await self.persona_handler.get_persona_prompt()
                    except Exception as e:
                        logger.warning(f"[ComplexSolver] 获取人设失败: {e}")
                
                # 调用精简模型精炼（去除思考痕迹，保留完整内容）
                refined_result = await self.summarizer.summarize_vision_result(
                    message, vision_result, sender_info, curr_cid, persona_prompt
                )
                
                final_answer = refined_result
                logger.info(f"[ComplexSolver] 精简模型处理完成，原文{len(vision_result)}字 -> 处理后{len(final_answer)}字")
                
                # 【关键】静默插入上下文（不触发输出）
                await self._insert_context_silently(
                    event, 
                    f"{message} [图片]", 
                    final_answer, 
                    curr_cid
                )
                
                # 发送最终结果
                await self._send_result(event, final_answer, sender_info)
                
                # 标记已处理，停止事件传播
                event.set_extra("_complex_solver_processed", True)
                event.stop_event()
                logger.info("[ComplexSolver] ===== 识图流程完成，已停止事件 =====")
                return
        
        # === 步骤3: 未触发识图或识图失败，判断问题复杂度 ===
        processed_message = message
        if vision_result and vision_result.startswith("["):
            # 识图失败但有错误信息，合并到消息中给主LLM参考
            processed_message = f"{message}\n\n[系统提示: 图片识别失败 - {vision_result}]"
        
        logger.info("[ComplexSolver] 未触发视觉识别或识别失败，判断问题复杂度...")
        
        # 检查是否为追问（更严格的检查）
        is_followup = await self.intent_judge.is_followup_question(
            processed_message, sender_info, curr_cid
        )
        
        if is_followup:
            logger.info("[ComplexSolver] 检测到追问，转给主LLM处理")
            # 追问不经过复杂求解器，直接放行
            return
        
        is_complex = await self.intent_judge.is_complex_question(
            processed_message, sender_info, curr_cid
        )
        
        logger.info(f"[ComplexSolver] 问题复杂度判断: {'复杂问题' if is_complex else '简单问题'}")
        
        if not is_complex:
            # 简单问题：添加Bot标签帮助触发主动回复，然后放行
            logger.info("[ComplexSolver] 简单问题，准备添加Bot标签后放行...")
            
            # 标记已处理，防止死循环
            event.set_extra("_complex_solver_processed", True)
            
            # 检查是否已包含Bot名字，如果没有则添加标签
            if not self._message_has_bot_name(message):
                bot_tag = self.bot_names[0] if self.bot_names else "Bot"
                tagged_message = f"{message} <{bot_tag}>"
                
                # 修改事件消息文本
                event.message_str = tagged_message
                logger.info(f"[ComplexSolver] 已添加Bot标签: {tagged_message[:50]}...")
                
                # 同时修改消息链（确保其他插件能看到）
                if hasattr(event, 'message_obj') and event.message_obj:
                    if hasattr(event.message_obj, 'message') and isinstance(event.message_obj.message, list):
                        from astrbot.api.message_components import Plain
                        msg_chain = event.message_obj.message
                        if msg_chain:
                            # 尝试在最后一个Plain组件追加，或添加新的
                            last_plain_idx = -1
                            for i, comp in enumerate(msg_chain):
                                if isinstance(comp, Plain):
                                    last_plain_idx = i
                            
                            if last_plain_idx >= 0:
                                msg_chain[last_plain_idx].text = f"{msg_chain[last_plain_idx].text} <{bot_tag}>"
                            else:
                                msg_chain.append(Plain(f"<{bot_tag}>"))
                        else:
                            msg_chain.append(Plain(f"<{bot_tag}>"))
            else:
                logger.info("[ComplexSolver] 消息已包含Bot名字，无需添加标签")
            
            # 放行给主LLM处理
            logger.info("[ComplexSolver] 放行给主LLM处理")
            return
        
        # === 步骤4: 复杂问题，调用解题模型 ===
        logger.info("[ComplexSolver] 复杂问题，进入解题流程...")
        
        # 检查问题明确性
        clarity_check = await self.intent_judge.check_question_clarity(
            processed_message, sender_info, curr_cid
        )
        
        if not clarity_check.get("is_clear", True):
            # 问题不明确，拒绝回答
            logger.warning(f"[ComplexSolver] 问题不明确: {clarity_check.get('reason', 'unknown')}")
            refusal_msg = await self.persona_handler.generate_refusal(
                main_provider_id,
                clarity_check.get('reason', '问题描述不够明确'),
                message,
                sender_info,
                curr_cid
            )
            await self._send_result(event, refusal_msg, sender_info)
            event.set_extra("_complex_solver_processed", True)
            event.stop_event()
            return
        
        # 检查解题模型配置
        if not self.solver_provider and not self.solver_provider_2:
            logger.error("[ComplexSolver] 解题模型未配置")
            apology = await self.persona_handler.generate_apology(
                main_provider_id,
                "解题服务未配置，请管理员配置解题模型",
                message,
                sender_info,
                curr_cid
            )
            await self._send_result(event, apology, sender_info)
            event.set_extra("_complex_solver_processed", True)
            event.stop_event()
            return
        
        # 获取历史消息
        history_messages = []
        if curr_cid:
            try:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation:
                    history_messages = conversation.history or []
                    logger.debug(f"[ComplexSolver] 获取到 {len(history_messages)} 条历史消息")
            except Exception as e:
                logger.warning(f"[ComplexSolver] 获取历史对话失败: {e}")
        
        # 调用解题模型（带重试机制）
        async def send_waiting_message(round_num: int):
            """发送等待消息回调"""
            waiting_msg = await self.persona_handler.generate_waiting_message(
                main_provider_id, round_num, message, sender_info, curr_cid
            )
            await event.send(event.plain_result(waiting_msg))
        
        # 如果已经用过视觉模型，不再传图片给solver（避免重复处理）
        images_for_solver = [] if vision_triggered else images
        
        logger.info(f"[ComplexSolver] 开始调用解题模型，图片数量: {len(images_for_solver)}...")
        solver_answer, success = await self.solver.solve_with_retry(
            processed_message,
            images_for_solver,
            history_messages,
            sender_info,
            curr_cid,
            waiting_callback=send_waiting_message if self.enable_context else None
        )
        
        if not success or not solver_answer:
            logger.error("[ComplexSolver] 解题模型未能在规定时间内返回有效结果")
            apology = await self.persona_handler.generate_apology(
                main_provider_id,
                f"解题助手在 {self.max_wait_minutes} 分钟内未能获取有效解答，请稍后重试或简化问题",
                message,
                sender_info,
                curr_cid
            )
            await self._send_result(event, apology, sender_info)
            event.set_extra("_complex_solver_processed", True)
            event.stop_event()
            return
        
        # 解题成功，经过后处理（去除思考痕迹）
        logger.info(f"[ComplexSolver] 解题模型返回成功，长度: {len(solver_answer)}，准备后处理...")
        
        persona_prompt = ""
        if self.enable_persona_restate:
            try:
                persona_prompt = await self.persona_handler.get_persona_prompt()
            except Exception as e:
                logger.warning(f"[ComplexSolver] 获取人设失败: {e}")
        
        final_answer = await self.summarizer.summarize(
            message, 
            solver_answer, 
            sender_info, 
            curr_cid,
            persona_prompt=persona_prompt,
            use_persona=self.enable_persona_restate
        )
        
        logger.info(f"[ComplexSolver] 解题结果处理后: {len(solver_answer)}字 -> {len(final_answer)}字")
        
        # 发送最终结果
        await self._send_result(event, final_answer, sender_info)
        
        # 静默插入上下文
        if curr_cid and self.enable_context:
            await self._insert_context_silently(event, message, final_answer, curr_cid)
        
        # 标记并停止
        event.set_extra("_complex_solver_processed", True)
        event.stop_event()
        logger.info("[ComplexSolver] ===== 解题流程完成 =====")

    async def _send_result(self, event: AstrMessageEvent, text: str, sender_info: dict):
        """发送结果"""
        import re
        
        if self.enable_at_mention and sender_info.get("group_id"):
            text = re.sub(r'\[at:[^\]]+\]', '', text).strip()
            text = f"[at:{sender_info['id']}] {text}"
        
        try:
            if self.enable_latex:
                has_latex = any(marker in text for marker in ['$$', r'\[', r'\]', r'\(', r'\)', '$'])
                render_whole = self.enable_whole_render and not has_latex
                
                if render_whole:
                    components = self.at_handler.process_at_tags(text)
                    at_components = [c for c in components if hasattr(c, 'qq')]
                    text_components = [c for c in components if not hasattr(c, 'qq')]
                    
                    if at_components:
                        await event.send(event.chain_result(at_components))
                    
                    remaining_text = ''.join([c.text for c in text_components if hasattr(c, 'text')])
                    if remaining_text.strip():
                        await self.latex_renderer.send_with_latex(event, remaining_text, render_whole=True)
                else:
                    await self.latex_renderer.send_with_latex(event, text, render_whole=False)
            else:
                await event.send(event.plain_result(text))
        except Exception as e:
            logger.error(f"[ComplexSolver] 发送失败: {e}")
            await event.send(event.plain_result(text))

    async def terminate(self):
        """清理"""
        try:
            self.latex_renderer.cleanup()
        except Exception as e:
            logger.error(f"[ComplexSolver] 清理失败: {e}")
        logger.info("[ComplexSolver] 已卸载")