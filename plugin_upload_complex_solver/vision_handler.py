"""
视觉识别模块
处理图片内容的识别，使用多模态模型分析图片
"""
from typing import Optional, List, Dict, Any

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter


class VisionHandler:
    """视觉处理器 - 用于识别图片内容"""
    
    def __init__(
        self,
        context: Context,
        debugger: DebuggerReporter,
        vision_provider: str,
        vision_model: Optional[str] = None
    ):
        self.context = context
        self.debugger = debugger
        self.vision_provider = vision_provider
        self.vision_model = vision_model
    
    async def analyze_images(
        self,
        images: List[str],
        user_question: str,
        sender_info: dict,
        conv_id: str
    ) -> str:
        """
        分析图片内容并返回描述
        
        Args:
            images: 图片URL列表
            user_question: 用户的问题（用于上下文理解）
            sender_info: 发送者信息
            conv_id: 会话ID
            
        Returns:
            图片内容描述文本
        """
        if not self.vision_provider:
            logger.warning("[VisionHandler] 未配置视觉模型，跳过图片识别")
            return ""
        
        if not images:
            logger.debug("[VisionHandler] 图片列表为空，跳过识别")
            return ""
        
        # 构建提示词
        prompt = f"""请详细描述这张图片的内容。用户的问题是："{user_question}"

请根据用户的问题，有针对性地描述图片中的关键信息：
- 如果是数学题，请描述题目内容、公式、图表等
- 如果是物理/化学题，请描述实验装置、图表、数据等
- 如果是其他类型的问题，请描述与问题相关的视觉信息

请用中文详细描述，确保解题模型能够理解图片内容。"""
        
        # 【调试信息】开始视觉识别流程
        logger.info(f"[VisionHandler] 开始视觉识别流程")
        logger.info(f"[VisionHandler] 视觉模型提供商: {self.vision_provider}")
        logger.info(f"[VisionHandler] 视觉模型名称: {self.vision_model or 'default'}")
        logger.info(f"[VisionHandler] 图片数量: {len(images)}")
        logger.debug(f"[VisionHandler] 发送者: {sender_info.get('name', 'Unknown')}({sender_info.get('id', 'N/A')})")
        logger.debug(f"[VisionHandler] 会话ID: {conv_id}")
        
        # 【调试信息】打印模型看到的具体内容
        logger.info(f"[VisionHandler] ===== 视觉模型输入内容 =====")
        logger.info(f"[VisionHandler] Prompt内容: {prompt}")
        for idx, img_url in enumerate(images, 1):
            # 截断过长的URL避免日志臃肿
            display_url = img_url[:100] + "..." if len(img_url) > 100 else img_url
            logger.info(f"[VisionHandler] 图片 {idx} URL: {display_url}")
        logger.info(f"[VisionHandler] ===== 视觉模型输入结束 =====")
        
        try:
            logger.info(f"[VisionHandler] 开始调用视觉模型 API...")
            
            # 上报请求
            await self.debugger.report_request(
                provider_id=self.vision_provider,
                model=self.vision_model or "unknown",
                prompt=prompt,
                images=images,
                purpose="vision_analysis",
                sender_info=sender_info,
                conv_id=conv_id,
                system_prompt="你是一个视觉分析助手，擅长准确描述图片中的文字、公式、图表等内容。",
                contexts=[]
            )
            
            # 调用视觉模型
            kwargs = {
                "chat_provider_id": self.vision_provider,
                "prompt": prompt,
                "image_urls": images
            }
            if self.vision_model:
                kwargs["model"] = self.vision_model
            
            logger.debug(f"[VisionHandler] API调用参数: {kwargs}")
            resp = await self.context.llm_generate(**kwargs)
            result = resp.completion_text.strip()
            
            # 【调试信息】记录模型返回结果
            logger.info(f"[VisionHandler] 视觉模型调用完成")
            logger.info(f"[VisionHandler] 返回结果长度: {len(result)} 字符")
            logger.info(f"[VisionHandler] 实际使用模型: {getattr(resp, 'model', self.vision_model or 'unknown')}")
            
            # 【调试信息】打印返回内容预览（前200字符）
            preview = result[:200] + "..." if len(result) > 200 else result
            logger.info(f"[VisionHandler] 返回内容预览: {preview}")
            
            # 上报响应
            await self.debugger.report_response(
                provider_id=self.vision_provider,
                model=getattr(resp, 'model', self.vision_model or 'unknown'),
                response=result,
                purpose="vision_analysis",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )
            
            if not result:
                logger.warning(f"[VisionHandler] 视觉模型返回空结果")
                return "[图片识别返回空结果]"
            
            logger.info(f"[VisionHandler] 图片识别成功完成")
            return result
            
        except Exception as e:
            logger.error(f"[VisionHandler] 图片识别失败: {e}")
            logger.error(f"[VisionHandler] 异常详情: {str(e)}")
            import traceback
            logger.debug(f"[VisionHandler] 异常堆栈: {traceback.format_exc()}")
            return f"[图片识别失败: {str(e)}]"
    
    def is_configured(self) -> bool:
        """检查是否配置了视觉模型"""
        return bool(self.vision_provider)