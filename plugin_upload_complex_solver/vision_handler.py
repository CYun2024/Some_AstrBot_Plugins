"""
视觉识别模块 - 3模型架构：OCR专用 + 多模态主备
"""
import asyncio
from typing import Optional, List

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter


class VisionHandler:
    """视觉处理器 - OCR专用 + 多模态主备"""

    def __init__(
        self,
        context: Context,
        debugger: DebuggerReporter,
        ocr_provider: Optional[str],
        scene_provider_1: Optional[str],
        scene_provider_2: Optional[str],
        timeout: int = 90
    ):
        self.context = context
        self.debugger = debugger
        self.ocr_provider = ocr_provider
        self.scene_provider_1 = scene_provider_1
        self.scene_provider_2 = scene_provider_2
        self.timeout = timeout

    def is_ocr_configured(self) -> bool:
        """检查是否配置了OCR专用模型"""
        return bool(self.ocr_provider)

    def is_scene_configured(self) -> bool:
        """检查是否配置了至少一个多模态模型"""
        return bool(self.scene_provider_1) or bool(self.scene_provider_2)

    async def ocr_extract(self, images: List[str], user_question: str, 
                         sender_info: dict, conv_id: str) -> str:
        """
        OCR专用模型提取文字
        """
        if not self.is_ocr_configured():
            logger.warning("[VisionHandler] OCR专用模型未配置")
            return ""

        prompt = f"""请精确识别图片中的文字内容。
要求：
1. 只输出图片中的文字，不要描述图片
2. 保持原文的排版和格式
3. 如果包含公式，请用LaTeX格式标注
4. 如果看不清或没有文字，回复"【无文字内容】"

用户问题: "{user_question}"

请只输出识别到的文字："""

        return await self._call_vision_model(
            provider_id=self.ocr_provider,
            images=images,
            prompt=prompt,
            purpose="ocr_extraction",
            sender_info=sender_info,
            conv_id=conv_id
        )

    async def scene_analyze(self, images: List[str], user_question: str,
                           sender_info: dict, conv_id: str) -> str:
        """
        多模态场景理解（带主备切换）
        """
        if not self.is_scene_configured():
            logger.warning("[VisionHandler] 多模态模型未配置")
            return ""

        prompt = f"""请描述这张图片的内容。

用户问题: "{user_question}"

请根据用户问题有针对性地描述：
- 如果是问"有什么"，请列出图片中的主要物体
- 如果是问"这是什么"，请识别并解释
- 如果是OCR失败转过来的，请尽可能猜测文字内容

请用中文详细描述："""

        # 先尝试主模型
        if self.scene_provider_1:
            logger.info(f"[VisionHandler] 尝试多模态主模型: {self.scene_provider_1}")
            result = await self._call_vision_model(
                provider_id=self.scene_provider_1,
                images=images,
                prompt=prompt,
                purpose="scene_analysis_primary",
                sender_info=sender_info,
                conv_id=conv_id
            )
            
            if result and not result.startswith("["):
                logger.info("[VisionHandler] 多模态主模型成功")
                return result
            
            logger.warning(f"[VisionHandler] 主模型失败: {result[:100]}...")
        
        # 主模型失败，尝试备用模型
        if self.scene_provider_2:
            logger.info(f"[VisionHandler] 切换到多模态备用模型: {self.scene_provider_2}")
            result = await self._call_vision_model(
                provider_id=self.scene_provider_2,
                images=images,
                prompt=prompt,
                purpose="scene_analysis_backup",
                sender_info=sender_info,
                conv_id=conv_id
            )
            
            if result and not result.startswith("["):
                logger.info("[VisionHandler] 多模态备用模型成功")
                return f"[已切换至备用视觉模型]\n{result}"
            
            logger.error(f"[VisionHandler] 备用模型也失败: {result[:100]}...")
            return f"[视觉识别失败] 主备多模态模型均不可用。错误: {result}"
        
        return "[视觉识别失败] 未配置可用的多模态模型"

    async def _call_vision_model(self, provider_id: str, images: List[str],
                                prompt: str, purpose: str, sender_info: dict,
                                conv_id: str) -> str:
        """通用视觉模型调用"""
        
        try:
            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
                "image_urls": images
            }

            await self.debugger.report_request(
                provider_id=provider_id,
                model="default",
                prompt=prompt,
                images=images,
                purpose=purpose,
                sender_info=sender_info,
                conv_id=conv_id,
                system_prompt="你是一个视觉分析助手。",
                contexts=[]
            )

            logger.debug(f"[VisionHandler] 调用 {provider_id}，超时: {self.timeout}s")
            start_time = asyncio.get_event_loop().time()
            
            resp = await asyncio.wait_for(
                self.context.llm_generate(**kwargs),
                timeout=self.timeout
            )
            
            elapsed = asyncio.get_event_loop().time() - start_time
            result = resp.completion_text.strip()
            
            logger.info(f"[VisionHandler] {provider_id} 完成，耗时: {elapsed:.1f}s, 返回长度: {len(result)}")

            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                response=result,
                purpose=purpose,
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )

            return result

        except asyncio.TimeoutError:
            logger.error(f"[VisionHandler] {provider_id} 超时({self.timeout}s)")
            return f"[视觉模型超时: {provider_id}]"
        except Exception as e:
            logger.error(f"[VisionHandler] {provider_id} 异常: {e}")
            return f"[视觉模型错误: {str(e)}]"