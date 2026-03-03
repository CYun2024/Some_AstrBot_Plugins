"""
AstrBot 复杂问题求解助手插件
通过强弱模型协作，自动分流复杂问题，人设化复述解答，支持多模态和LaTeX渲染。
支持双模型并行调用、超时重试机制、独立精简模型、@提及功能。

版本: 3.1.1
作者: CYun2024
"""

from pathlib import Path
from typing import Dict, Any, List
import json

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
from .question_judge import QuestionJudge
from .utils import extract_images
from .vision_handler import VisionHandler


@register(
    name="complex_solver",
    author="CYun2024",
    desc="多模型协作求解插件，自动识别复杂问题并调用专业模型，支持@提及和双模型并行",
    version="3.1.1",
    repo="https://github.com/CYun2024"
)
class ComplexSolverPlugin(Star):
    """复杂问题求解助手 - 支持@提及和双模型并行版"""

    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        self.context = context

        # 加载配置（防止None）
        config = config or {}

        # 解题模型配置
        self.solver_provider = config.get("solver_provider", "")
        self.solver_model = config.get("solver_model", "")
        self.solver_provider_2 = config.get("solver_provider_2", "")
        self.solver_model_2 = config.get("solver_model_2", "")

        # 视觉模型配置
        self.vision_provider = config.get("vision_provider", "")
        self.vision_model = config.get("vision_model", "")
        self.enable_vision = config.get("enable_vision", True)

        # 精简模型配置
        self.summarize_provider = config.get("summarize_provider", "")
        self.summarize_model = config.get("summarize_model", "")

        # 功能开关配置
        self.enable_latex = config.get("enable_latex_render", True)
        self.enable_context = config.get("enable_context", True)
        self.enable_summarize = config.get("enable_summarize", True)
        self.enable_persona_restate = config.get("enable_persona_restate", True)
        self.enable_at_mention = config.get("enable_at_mention", True)
        self.enable_whole_render = config.get("enable_whole_render", True)
        
        # 调试配置（必须有这两行）
        self.strict_trigger = config.get("strict_trigger", True)
        self.debug_mode = config.get("debug_mode", False)

        # Bot名称配置
        self.bot_names = config.get("bot_names", ["韶梦", "shaomeng", "bot"])

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
        logger.info(f"ComplexSolverPlugin v3.1.1 已加载，{mode_str}模式，{persona_str}，{at_str}")
        
        if self.debug_mode:
            logger.info(f"[ComplexSolver] 调试模式已开启，Bot名字列表: {self.bot_names}")
            logger.info(f"[ComplexSolver] 严格触发模式: {'开启' if self.strict_trigger else '关闭'}")
    
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

        # 视觉处理器
        self.vision_handler = VisionHandler(
            self.context,
            self.debugger,
            self.vision_provider,
            self.vision_model
        )

    def _should_process_message(self, event: AstrMessageEvent, message: str) -> bool:
        """
        判断是否应该处理该消息
        只在以下情况处理：
        1. 私聊环境
        2. 消息中包含Bot名字（如"韶梦"）
        3. 消息中明确@了Bot
        """
        sender_name = event.get_sender_name()
        
        # 1. 检查是否是私聊（group_id 为 None 或空字符串）
        try:
            group_id = event.get_group_id()
            is_private = not group_id or str(group_id) == "None" or str(group_id) == ""
        except Exception:
            is_private = True  # 如果获取失败，假设是私聊
        
        if is_private:
            logger.info(f"[ComplexSolver] 私聊消息来自 {sender_name}，允许处理")
            return True
        
        # 2. 检查是否包含Bot名字（不区分大小写）
        message_lower = message.lower()
        logger.debug(f"[ComplexSolver] 检查消息内容: {message[:50]}...")
        logger.debug(f"[ComplexSolver] 配置的Bot名字: {self.bot_names}")
        
        for name in self.bot_names:
            if name.lower() in message_lower:
                logger.info(f"[ComplexSolver] 检测到Bot名字 '{name}'，允许处理")
                return True
        
        # 3. 检查是否被@（At组件）- 多种方式尝试获取self_id
        try:
            self_id = None
            
            # 方式1: event.get_self_id()
            if hasattr(event, 'get_self_id'):
                try:
                    self_id = event.get_self_id()
                except:
                    pass
            
            # 方式2: context.get_self_id() 或类似方法
            if not self_id and hasattr(self.context, 'get_self_id'):
                try:
                    self_id = self.context.get_self_id()
                except:
                    pass
            
            # 方式3: 从context的provider获取（某些平台）
            if not self_id:
                try:
                    # 尝试获取当前provider的self_id
                    umo = event.unified_msg_origin
                    if umo and ':' in str(umo):
                        parts = str(umo).split(':')
                        if len(parts) >= 2:
                            self_id = parts[1]  # 通常是 platform:self_id:group:...
                except:
                    pass
            
            logger.debug(f"[ComplexSolver] Bot ID: {self_id}")
            
            # 检查消息中的@组件
            if event.message_obj and hasattr(event.message_obj, 'message'):
                msg_chain = event.message_obj.message
                logger.debug(f"[ComplexSolver] 消息链组件数: {len(msg_chain) if msg_chain else 0}")
                
                for idx, comp in enumerate(msg_chain):
                    logger.debug(f"[ComplexSolver] 组件{idx}: {type(comp).__name__}")
                    
                    if isinstance(comp, At):
                        target_id = str(comp.qq) if hasattr(comp, 'qq') else str(comp.target) if hasattr(comp, 'target') else None
                        logger.debug(f"[ComplexSolver] 检测到@，目标ID: {target_id}, Bot ID: {self_id}")
                        
                        if target_id and self_id and target_id == str(self_id):
                            logger.info(f"[ComplexSolver] 检测到被@{target_id}，允许处理")
                            return True
                        elif not self_id:
                            # 如果无法获取self_id，只要有@就认为是@Bot（在私聊已过滤的情况下）
                            logger.info(f"[ComplexSolver] 检测到@(无法确认目标)，允许处理")
                            return False
        except Exception as e:
            logger.warning(f"[ComplexSolver] 检查@时出错: {e}")
            import traceback
            logger.debug(traceback.format_exc())
        
        logger.info(f"[ComplexSolver] 不满足处理条件，跳过（群聊且无Bot名字且无@）")
        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def handle_message(self, event: AstrMessageEvent):
        """主消息处理流程"""
        message = event.message_str.strip()
        if not message:
            return
        
        # 【临时诊断代码】打印消息对象结构
        logger.info(f"========== 消息诊断开始 ==========")
        logger.info(f"平台: {event.get_platform_name()}")
        logger.info(f"消息内容: {message}")
        
        if hasattr(event, 'message_obj') and event.message_obj:
            msg_obj = event.message_obj
            
            # 打印所有属性
            attrs = [attr for attr in dir(msg_obj) if not attr.startswith('_')]
            logger.info(f"消息对象属性: {attrs}")
            
            # 检查 raw_message (aiocqhttp 中是 Event 对象)
            if hasattr(msg_obj, 'raw_message'):
                raw = msg_obj.raw_message
                logger.info(f"raw_message 类型: {type(raw)}")
                
                # 如果是 Event 对象，打印其属性
                if hasattr(raw, '__dict__'):
                    logger.info(f"raw_message (Event) 属性: {list(raw.__dict__.keys())}")
                    # 打印内容（注意不要打印太敏感的信息）
                    try:
                        raw_dict = {k: v for k, v in raw.__dict__.items() if k not in ['sender', 'self_id']}
                        logger.info(f"raw_message 内容预览: {str(raw_dict)[:500]}")
                    except:
                        pass
                elif isinstance(raw, dict):
                    logger.info(f"raw_message (dict): {json.dumps(raw, ensure_ascii=False, indent=2)[:500]}")
                elif isinstance(raw, str):
                    logger.info(f"raw_message (str): {raw[:200]}")
            
            # 检查 message 属性（可能是数组）
            if hasattr(msg_obj, 'message'):
                msg_chain = msg_obj.message
                logger.info(f"message 类型: {type(msg_chain)}")
                if isinstance(msg_chain, list):
                    logger.info(f"message 数组长度: {len(msg_chain)}")
                    for i, item in enumerate(msg_chain):
                        item_type = type(item).__name__
                        logger.info(f"  [{i}] 类型: {item_type}")
                        # 如果是字典或对象，打印内容
                        if hasattr(item, '__dict__'):
                            logger.info(f"       内容: {item.__dict__}")
                        elif isinstance(item, dict):
                            logger.info(f"       内容: {item}")
                        # 检查是否是 Reply
                        if 'Reply' in item_type or (isinstance(item, dict) and item.get('type') == 'reply'):
                            logger.info(f"       !! 发现 Reply 组件 !!")
            
            # 检查 group 相关属性（引用消息可能在其中）
            if hasattr(msg_obj, 'group_id'):
                logger.info(f"group_id: {msg_obj.group_id}")
        
        logger.info(f"========== 消息诊断结束 ==========")
        # 【诊断代码结束】

        # 检查是否应该处理该消息（防止在群聊中误触发）
        if self.strict_trigger:
            if not self._should_process_message(event, message):
                return
        else:
            logger.info("[ComplexSolver] 严格触发模式已关闭，处理所有消息")
        
        # 1. 提取所有图片（包括当前消息和引用的消息）
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
        
        # 2. 【关键】先处理图片：如果有图片，先进行视觉分析，插入消息末尾
        processed_message = message
        image_description = ""  # 用于后续兜底逻辑判断
        
        # 【调试信息】详细记录消息对象结构
        logger.debug(f"[ComplexSolver] 开始处理消息，平台: {event.get_platform_name()}")
        if hasattr(event, 'message_obj') and event.message_obj:
            msg_obj = event.message_obj
            logger.debug(f"[ComplexSolver] 消息对象类型: {type(msg_obj).__name__}")
            
            # 检查是否为引用消息
            is_reply = False
            if hasattr(msg_obj, 'reply') and msg_obj.reply:
                is_reply = True
                logger.info(f"[ComplexSolver] 检测到引用消息 (reply)")
            if hasattr(msg_obj, 'referenced_message') and msg_obj.referenced_message:
                is_reply = True
                logger.info(f"[ComplexSolver] 检测到引用消息 (referenced_message)")
            if hasattr(msg_obj, 'raw_message') and isinstance(msg_obj.raw_message, dict):
                raw = msg_obj.raw_message
                if raw.get('message') and isinstance(raw['message'], list):
                    for item in raw['message']:
                        if isinstance(item, dict) and item.get('type') == 'reply':
                            is_reply = True
                            logger.info(f"[ComplexSolver] 检测到引用消息 (raw_message.reply)")
                            break
            
            if not is_reply:
                logger.debug(f"[ComplexSolver] 非引用消息")
        
        if images:
            logger.info(f"[ComplexSolver] 检测到 {len(images)} 张图片（含引用），准备进行视觉识别...")
            logger.debug(f"[ComplexSolver] 图片URL列表: {[url[:50]+'...' if len(url)>50 else url for url in images]}")
            
            if self.enable_vision and self.vision_handler.is_configured():
                logger.info(f"[ComplexSolver] 视觉功能已启用，视觉模型配置: {self.vision_provider}/{self.vision_model or 'default'}")
                image_description = await self.vision_handler.analyze_images(
                    images, message, sender_info, curr_cid
                )
                if image_description:
                    # 将图片识别结果附加到用户问题末尾
                    processed_message = f"{message}\n\n[图片内容描述：{image_description}]"
                    logger.info(f"[ComplexSolver] 图片识别完成，已附加到问题末尾")
                    logger.debug(f"[ComplexSolver] 增强后问题前200字符：{processed_message[:200]}...")
                else:
                    logger.warning(f"[ComplexSolver] 视觉模型返回空描述，将使用原始问题文本")
                    # 【关键】即使视觉返回空，只要有图片且用户询问内容，也尝试处理
                    if any(kw in message for kw in ['图', '图片', '写', '内容', '什么']):
                        logger.info(f"[ComplexSolver] 用户询问图片内容但视觉返回空，标记为需处理")
            else:
                vision_status = "未启用" if not self.enable_vision else "未配置"
                logger.info(f"[ComplexSolver] 检测到{len(images)}张图片，但视觉功能{vision_status}，跳过分析")
                # 【关键】没开视觉但用户问图片内容，尝试用解题模型直接看
                if images and any(kw in message for kw in ['图', '图片', '写', '内容', '什么']):
                    logger.info(f"[ComplexSolver] 用户询问图片内容，尝试直接将图片传给解题模型")
                    processed_message = f"{message}\n\n[用户发送了图片，请分析图片内容回答问题]"
        else:
            logger.info(f"[ComplexSolver] 未检测到图片，跳过视觉识别")
            
        # 3. 【关键】用"增强后的消息"（含图片描述）判断是否为复杂问题
        # 【调试信息】显示判断用的最终文本
        logger.info(f"[ComplexSolver] 准备判断问题复杂度，使用文本长度: {len(processed_message)}")
        logger.debug(f"[ComplexSolver] 判断用文本预览: {processed_message[:150]}...")
        
        # 如果已经分析了图片并生成了描述，就不再给判断模型传原图（避免重复处理）
        images_for_judge = [] if image_description else images
        
        try:
            is_complex = await self.question_judge.is_complex_question(
                main_provider_id, 
                processed_message,  # 使用包含图片描述的消息
                images_for_judge,   # 如果已分析则为空，否则传原图给多模态模型判断
                sender_info, 
                conv_id=curr_cid
            )
            
            # 4. 兜底逻辑：如果用户发了图片且图片描述包含学术关键词，强制判定为复杂问题
            if not is_complex and image_description:
                academic_keywords = ['写', '求解', '计算', '证明', '公式', '方程', '积分', '导数', '矩阵', '函数', '几何', '物理', '化学', '代码', '编程', '算法', '步骤', '问', '解']
                if any(kw in image_description for kw in academic_keywords):
                    logger.info("[ComplexSolver] 虽然判断为简单问题，但图片内容包含学术关键词，强制启用解题模式")
                    is_complex = True
            
            # 【新增兜底】如果用户明确问图片写了什么，强制为复杂问题
            if not is_complex and images and any(kw in message for kw in ['写的是什么', '写的什么', '内容', '上面写着']):
                logger.info("[ComplexSolver] 用户询问图片文字内容，强制启用解题模式")
                is_complex = True
                    
        except Exception as e:
            logger.error(f"[ComplexSolver] 判断复杂问题时出错: {e}")
            return
        
        # 5. 判断是否为追问（使用原始message判断，因为追问通常很短，不需要图片描述）
        if not is_complex and self.enable_context:
            try:
                history = []
                if curr_cid:
                    conversation = await conv_mgr.get_conversation(umo, curr_cid)
                    if conversation:
                        history = conversation.history or []
                
                is_followup = await self.question_judge.is_followup_question(
                    main_provider_id, history, message, sender_info, curr_cid  # 注意这里用原始message
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
        
        # 如果已经用视觉模型处理了图片，就不传原图给解题模型
        # 解题模型只接收文本（包含图片描述）
        images_for_solver = [] if image_description else images
        
        solver_answer, success = await self.solver.solve_with_retry(
            processed_message,  # 使用包含图片描述的文本
            images_for_solver,   # 视觉模型已处理过则传空列表
            history_messages, 
            sender_info, 
            curr_cid,
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
    
    async def _send_result(self, event: AstrMessageEvent, text: str, sender_info: dict, use_whole_render: bool = False):
        """发送结果，处理@提及和LaTeX渲染"""
        import re
        
        # 自动@处理
        if self.enable_at_mention and sender_info.get("group_id"):
            text = re.sub(r'\[at:[^\]]+\]', '', text).strip()
            text = f"[at:{sender_info['id']}] {text}"
            logger.info(f"[SendResult] 已自动添加@用户: {sender_info['id']}")
        
        # 发送消息
        try:
            if self.enable_latex:
                # 【修复】检测所有LaTeX格式：$$, \[, \], \(, \), $
                has_latex = any(marker in text for marker in ['$$', r'\[', r'\]', r'\(', r'\)', '$'])
                
                # 判断是否使用整段渲染
                render_whole = use_whole_render or (not self.enable_persona_restate and self.enable_whole_render)
                
                # 如果包含复杂LaTeX公式，强制使用分段渲染（整段渲染对复杂公式支持不好）
                if render_whole and ('$$' in text or r'\[' in text):
                    logger.info("[SendResult] 检测到复杂公式，切换到分段渲染模式")
                    render_whole = False

                if render_whole and not has_latex:
                    # 纯文本整段渲染
                    components = self.at_handler.process_at_tags(text)
                    at_components = [c for c in components if hasattr(c, 'qq')]
                    text_components = [c for c in components if not hasattr(c, 'qq')]
                    
                    if at_components:
                        await event.send(event.chain_result(at_components))
                    
                    remaining_text = ''.join([c.text for c in text_components if hasattr(c, 'text')])
                    if remaining_text.strip():
                        await self.latex_renderer.send_with_latex(event, remaining_text, render_whole=True)
                else:
                    # 【关键】有LaTeX时，使用分段渲染（更稳定）
                    logger.info(f"[SendResult] 检测到LaTeX公式，使用分段渲染: {has_latex}")
                    await self.latex_renderer.send_with_latex(event, text, render_whole=False)
            else:
                # 不渲染LaTeX
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