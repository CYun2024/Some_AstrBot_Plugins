"""模型调用工具模块 - 支持主备故障转移"""

import asyncio
import time
import uuid
from typing import Any, Callable, Optional

from astrbot.api import logger
from astrbot.api.provider import Provider


class ModelCallResult:
    """模型调用结果"""
    def __init__(
        self,
        success: bool,
        text: str = "",
        error: str = "",
        provider_id: str = "",
        is_fallback: bool = False,
        usage: Any = None
    ):
        self.success = success
        self.text = text
        self.error = error
        self.provider_id = provider_id
        self.is_fallback = is_fallback
        self.usage = usage


async def call_model_with_fallback(
    context,
    config,
    primary_provider_id: str,
    fallback_provider_id: str,
    prompt: str,
    timeout_sec: float,
    record_callback: Optional[Callable] = None,
    purpose: str = "unknown",
    **kwargs
) -> ModelCallResult:
    """
    调用模型，支持主备故障转移

    Args:
        context: AstrBot上下文
        config: 配置对象
        primary_provider_id: 主模型提供商ID
        fallback_provider_id: 备用模型提供商ID
        prompt: 提示词
        timeout_sec: 超时时间
        record_callback: 可选的上报回调函数
        purpose: 调用目的标识
        **kwargs: 额外的请求参数

    Returns:
        ModelCallResult: 调用结果
    """
    conv_id = uuid.uuid4().hex

    async def try_call_provider(provider_id: str, is_fallback: bool) -> ModelCallResult:
        """尝试调用指定提供商"""
        provider = None
        actual_id = provider_id or "default"

        try:
            # 获取提供商
            if provider_id:
                provider = context.get_provider_by_id(provider_id)
            else:
                provider = context.get_using_provider()

            if not provider:
                return ModelCallResult(
                    success=False,
                    error=f"提供商 {actual_id} 不可用",
                    provider_id=actual_id,
                    is_fallback=is_fallback
                )

            model_name = getattr(provider, 'model', 'unknown')

            # 上报请求
            if record_callback:
                await record_callback({
                    "phase": "request",
                    "provider_id": actual_id,
                    "model": model_name,
                    "prompt": prompt[:500] + "..." if len(prompt) > 500 else prompt,
                    "source": {"plugin": "morechatplus", "purpose": purpose},
                    "conversation_id": conv_id,
                    "timestamp": time.time(),
                    "is_fallback": is_fallback
                })

            # 执行调用
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=conv_id,
                    persist=False,
                    **kwargs
                ),
                timeout=timeout_sec
            )

            text = response.completion_text or ""
            usage = getattr(response, 'usage', None)

            # 上报成功响应
            if record_callback:
                await record_callback({
                    "phase": "response",
                    "provider_id": actual_id,
                    "model": model_name,
                    "response": text[:200] + "..." if len(text) > 200 else text,
                    "usage": usage,
                    "source": {"plugin": "morechatplus", "purpose": purpose},
                    "conversation_id": conv_id,
                    "timestamp": time.time(),
                    "is_fallback": is_fallback,
                    "status": "success"
                })

            return ModelCallResult(
                success=True,
                text=text,
                provider_id=actual_id,
                is_fallback=is_fallback,
                usage=usage
            )

        except asyncio.TimeoutError:
            error_msg = f"调用超时 ({timeout_sec}秒)"
            logger.warning(f"[MoreChatPlus] 模型 {actual_id} 调用超时")

            if record_callback:
                await record_callback({
                    "phase": "response",
                    "provider_id": actual_id,
                    "model": getattr(provider, 'model', 'unknown') if provider else "unknown",
                    "response": f"[{error_msg}]",
                    "source": {"plugin": "morechatplus", "purpose": f"{purpose}_timeout"},
                    "conversation_id": conv_id,
                    "timestamp": time.time(),
                    "is_fallback": is_fallback,
                    "status": "error"
                })

            return ModelCallResult(
                success=False,
                error=error_msg,
                provider_id=actual_id,
                is_fallback=is_fallback
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[MoreChatPlus] 模型 {actual_id} 调用失败: {e}")

            if record_callback:
                await record_callback({
                    "phase": "response",
                    "provider_id": actual_id,
                    "model": getattr(provider, 'model', 'unknown') if provider else "unknown",
                    "response": f"[错误: {error_msg}]",
                    "source": {"plugin": "morechatplus", "purpose": f"{purpose}_error"},
                    "conversation_id": conv_id,
                    "timestamp": time.time(),
                    "is_fallback": is_fallback,
                    "status": "error"
                })

            return ModelCallResult(
                success=False,
                error=error_msg,
                provider_id=actual_id,
                is_fallback=is_fallback
            )

    # 首先尝试主模型
    result = await try_call_provider(primary_provider_id, is_fallback=False)

    # 如果主模型失败且有备用模型配置，尝试备用模型
    if not result.success and fallback_provider_id:
        logger.info(f"[MoreChatPlus] 主模型 {primary_provider_id or 'default'} 失败，切换到备用模型 {fallback_provider_id}")
        result = await try_call_provider(fallback_provider_id, is_fallback=True)

    return result