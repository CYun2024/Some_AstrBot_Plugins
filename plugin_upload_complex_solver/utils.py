"""
工具函数模块
"""
import re
from datetime import datetime
from typing import List, Dict, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def extract_images(event: AstrMessageEvent) -> List[str]:
    """从事件中提取图片"""
    try:
        return event.get_images()
    except AttributeError:
        return []


def build_history_messages(history_messages: List, enable_context: bool, max_history: int = 4) -> List[str]:
    """构建历史消息列表"""
    messages = []
    
    if history_messages and enable_context:
        for msg in history_messages[-max_history:]:
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
    
    return messages


def make_serializable(obj: Any) -> Any:
    """将对象转换为可序列化的格式"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple, set)):
        return [make_serializable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: make_serializable(value) for key, value in obj.items()}
    if hasattr(obj, 'model_dump'):
        return make_serializable(obj.model_dump())
    if hasattr(obj, 'dict'):
        return make_serializable(obj.dict())
    if hasattr(obj, '__dict__'):
        return make_serializable(obj.__dict__)
    return str(obj)


def format_timestamp() -> str:
    """获取当前时间戳"""
    return datetime.now().isoformat()


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """截断文本"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + suffix
