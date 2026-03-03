"""
工具函数模块 - 修复版，正确处理 Reply.chain 中的 Image 对象
"""
import re
import json
from datetime import datetime
from typing import List, Dict, Any, Union

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def extract_images(event: AstrMessageEvent) -> List[str]:
    """
    从事件中提取图片，包括当前消息和引用的消息
    针对 aiocqhttp 平台特别优化
    """
    images = []
    platform = event.get_platform_name()
    
    logger.info(f"[ExtractImages] ===== 开始提取图片 =====")
    logger.info(f"[ExtractImages] 平台: {platform}")
    logger.info(f"[ExtractImages] 消息内容: {event.message_str[:50]}...")
    
    # 1. 提取当前消息的图片（标准方法）
    try:
        current_images = event.get_images()
        if current_images:
            logger.info(f"[ExtractImages] 标准方法提取到 {len(current_images)} 张图片")
            images.extend(current_images)
    except Exception as e:
        logger.debug(f"[ExtractImages] 标准方法失败: {e}")
    
    # 2. 深度检查消息对象结构
    if not hasattr(event, 'message_obj') or not event.message_obj:
        logger.warning(f"[ExtractImages] 消息对象不存在")
        return images
    
    msg_obj = event.message_obj
    logger.debug(f"[ExtractImages] 消息对象类型: {type(msg_obj)}")
    
    # 3. 检查 raw_message（最原始的格式，包含 CQ 码或 JSON）
    if hasattr(msg_obj, 'raw_message'):
        raw = msg_obj.raw_message
        logger.debug(f"[ExtractImages] raw_message 类型: {type(raw)}")
        
        if isinstance(raw, dict):
            # JSON 格式消息
            logger.debug(f"[ExtractImages] raw_message 字典键: {list(raw.keys())}")
            
            # 检查 message 数组
            if 'message' in raw and isinstance(raw['message'], list):
                msg_array = raw['message']
                logger.info(f"[ExtractImages] 发现消息数组，长度: {len(msg_array)}")
                
                for idx, segment in enumerate(msg_array):
                    if not isinstance(segment, dict):
                        continue
                    
                    seg_type = segment.get('type', 'unknown')
                    logger.debug(f"[ExtractImages] 消息段 {idx}: type={seg_type}")
                    
                    if seg_type == 'image':
                        # 提取图片 URL
                        data = segment.get('data', {})
                        url = data.get('url') or data.get('file') or data.get('path')
                        if url:
                            logger.info(f"[ExtractImages] 从消息数组提取图片: {url[:60]}...")
                            images.append(url)
                    
                    elif seg_type == 'reply':
                        # 引用消息！需要深入检查
                        logger.info(f"[ExtractImages] 发现引用消息段，开始提取引用内容...")
                        reply_data = segment.get('data', {})
                        
                        # 有些平台引用消息直接包含被引用消息的内容
                        if 'message' in reply_data:
                            reply_msg = reply_data['message']
                            logger.debug(f"[ExtractImages] 引用消息内容类型: {type(reply_msg)}")
                            
                            if isinstance(reply_msg, list):
                                # 被引用消息是数组格式
                                imgs = _extract_from_array(reply_msg, f"reply[{idx}]")
                                images.extend(imgs)
                            elif isinstance(reply_msg, str):
                                # 被引用消息是字符串（CQ码格式）
                                cq_images = _extract_cq_images(reply_msg)
                                if cq_images:
                                    logger.info(f"[ExtractImages] 从引用消息 CQ 码提取 {len(cq_images)} 张图片")
                                    images.extend(cq_images)
            
            # 检查单独的 reply 字段（有些版本在这里）
            if 'reply' in raw and isinstance(raw['reply'], dict):
                logger.info(f"[ExtractImages] 发现独立 reply 字段")
                reply_msg = raw['reply'].get('message', [])
                if isinstance(reply_msg, list):
                    imgs = _extract_from_array(reply_msg, "raw_message.reply")
                    images.extend(imgs)
        
        elif isinstance(raw, str):
            # CQ 码格式字符串 [CQ:image,...]
            logger.debug(f"[ExtractImages] raw_message 是字符串，尝试解析 CQ 码")
            cq_images = _extract_cq_images(raw)
            if cq_images:
                logger.info(f"[ExtractImages] 从 CQ 码提取 {len(cq_images)} 张图片")
                images.extend(cq_images)
    
    # 4. 检查 reply 属性（对象格式）
    if hasattr(msg_obj, 'reply') and msg_obj.reply:
        logger.info(f"[ExtractImages] 检查 msg_obj.reply 属性")
        reply_obj = msg_obj.reply
        
        # 尝试各种可能的属性路径
        if hasattr(reply_obj, 'message'):
            reply_imgs = _extract_images_from_obj(reply_obj.message, "reply.message")
            images.extend(reply_imgs)
        if hasattr(reply_obj, 'raw_message'):
            reply_raw = reply_obj.raw_message
            if isinstance(reply_raw, str):
                cq_imgs = _extract_cq_images(reply_raw)
                if cq_imgs:
                    logger.info(f"[ExtractImages] 从 reply.raw_message 提取 {len(cq_imgs)} 张")
                    images.extend(cq_imgs)
            elif isinstance(reply_raw, dict) and 'message' in reply_raw:
                reply_imgs = _extract_from_array(reply_raw['message'], "reply.raw_message.message")
                images.extend(reply_imgs)
    
    # 5. 检查 referenced_message
    if hasattr(msg_obj, 'referenced_message') and msg_obj.referenced_message:
        logger.info(f"[ExtractImages] 检查 referenced_message 属性")
        ref_imgs = _extract_images_from_obj(msg_obj.referenced_message, "referenced_message")
        images.extend(ref_imgs)
    
    # 6. 【关键】检查 message 链中的 Reply 组件 - 特别是 chain 属性
    if hasattr(msg_obj, 'message') and msg_obj.message:
        logger.debug(f"[ExtractImages] 检查消息链中的 Reply 组件")
        for idx, comp in enumerate(msg_obj.message):
            comp_type = type(comp).__name__
            logger.debug(f"[ExtractImages] 组件 {idx}: {comp_type}")
            
            # 检查是否是 Reply 组件
            if 'Reply' in comp_type:
                logger.info(f"[ExtractImages] !! 发现 Reply 组件，索引: {idx} !!")
                
                # 检查 chain 属性（Reply 组件包含被引用消息的内容）
                if hasattr(comp, 'chain') and comp.chain:
                    logger.info(f"[ExtractImages] Reply.chain 类型: {type(comp.chain)}, 长度: {len(comp.chain)}")
                    # 直接遍历 chain 列表，提取 Image 对象
                    for c_idx, chain_item in enumerate(comp.chain):
                        chain_item_type = type(chain_item).__name__
                        logger.debug(f"[ExtractImages]   chain[{c_idx}]: {chain_item_type}")
                        
                        # 检查是否是 Image 对象或字典
                        if 'Image' in chain_item_type:
                            url = _extract_url_from_image_obj(chain_item, f"Reply.chain[{c_idx}]")
                            if url:
                                images.append(url)
                        elif isinstance(chain_item, dict) and chain_item.get('type') == 'image':
                            data = chain_item.get('data', {})
                            url = data.get('url') or data.get('file')
                            if url:
                                logger.info(f"[ExtractImages] 从 Reply.chain[{c_idx}] (dict) 提取: {url[:50]}...")
                                images.append(url)
                
                # 也检查其他可能包含图片的属性
                if hasattr(comp, 'message'):
                    inner_imgs = _extract_images_from_obj(comp.message, f"message[{idx}].message")
                    images.extend(inner_imgs)
    
    # 去重
    unique_images = list(dict.fromkeys(images))
    
    if len(unique_images) != len(images):
        logger.debug(f"[ExtractImages] 去重: {len(images)} -> {len(unique_images)}")
    
    logger.info(f"[ExtractImages] ===== 提取完成，共 {len(unique_images)} 张图片 =====")
    return unique_images


def _extract_url_from_image_obj(obj, source: str) -> str:
    """从 Image 对象中提取 URL"""
    try:
        # 尝试各种可能的属性
        url = None
        if hasattr(obj, 'url') and obj.url:
            url = obj.url
        elif hasattr(obj, 'file') and obj.file:
            url = obj.file
        elif hasattr(obj, 'path') and obj.path:
            url = obj.path
        elif hasattr(obj, 'data') and isinstance(obj.data, dict):
            url = obj.data.get('url') or obj.data.get('file')
        
        if url:
            display_url = url[:60] + "..." if len(url) > 60 else url
            logger.info(f"[ExtractImages] 从 {source} (Image对象) 提取: {display_url}")
            return url
    except Exception as e:
        logger.debug(f"[ExtractImages] 从 {source} 提取失败: {e}")
    return ""


def _extract_cq_images(cq_string: str) -> List[str]:
    """从 CQ 码字符串中提取图片 URL"""
    images = []
    if not cq_string or '[CQ:' not in cq_string:
        return images
    
    # 匹配 [CQ:image,file=xxx,url=xxx] 或 [CQ:image,file=xxx]
    # 支持 url= 或 file= 参数
    pattern = r'\[CQ:image,([^\]]+)\]'
    matches = re.findall(pattern, cq_string)
    
    for params in matches:
        # 提取 url 参数
        url_match = re.search(r'url=([^,\]]+)', params)
        if url_match:
            url = url_match.group(1).strip()
            # 处理转义字符
            url = url.replace('&#44;', ',').replace('&#91;', '[').replace('&#93;', ']')
            images.append(url)
        else:
            # 如果没有 url，尝试 file 参数（有时是本地路径或缓存键）
            file_match = re.search(r'file=([^,\]]+)', params)
            if file_match:
                file_val = file_match.group(1).strip()
                file_val = file_val.replace('&#44;', ',').replace('&#91;', '[').replace('&#93;', ']')
                # 如果是 URL 格式也加入
                if file_val.startswith('http'):
                    images.append(file_val)
    
    return images


def _extract_from_array(msg_array: List[Dict], source: str) -> List[str]:
    """从消息数组中提取图片（处理字典和对象）"""
    images = []
    if not isinstance(msg_array, list):
        return images
    
    for idx, seg in enumerate(msg_array):
        # 处理字典格式
        if isinstance(seg, dict):
            if seg.get('type') == 'image':
                data = seg.get('data', {})
                url = data.get('url') or data.get('file')
                if url:
                    logger.info(f"[ExtractImages] 从 {source}[{idx}] (dict) 提取: {url[:50]}...")
                    images.append(url)
        # 处理对象格式（如 Image 组件对象）
        else:
            seg_type = type(seg).__name__
            if 'Image' in seg_type:
                url = _extract_url_from_image_obj(seg, f"{source}[{idx}]")
                if url:
                    images.append(url)
    return images


def _extract_images_from_obj(obj, source: str) -> List[str]:
    """通用对象图片提取器"""
    images = []
    try:
        logger.debug(f"[_ExtractObj] 从 {source} 提取，类型: {type(obj)}")
        
        if hasattr(obj, 'get_images'):
            try:
                imgs = obj.get_images()
                if imgs:
                    logger.info(f"[_ExtractObj] {source}.get_images() -> {len(imgs)} 张")
                    images.extend(imgs)
            except Exception as e:
                logger.debug(f"[_ExtractObj] {source}.get_images() 失败: {e}")
        
        if hasattr(obj, 'message'):
            msg = obj.message
            if isinstance(msg, list):
                imgs = _extract_from_array(msg, f"{source}.message")
                images.extend(imgs)
            elif isinstance(msg, str):
                imgs = _extract_cq_images(msg)
                if imgs:
                    logger.info(f"[_ExtractObj] 从 {source}.message CQ 码提取 {len(imgs)} 张")
                    images.extend(imgs)
        
        if isinstance(obj, dict):
            if 'message' in obj and isinstance(obj['message'], list):
                imgs = _extract_from_array(obj['message'], f"{source}[dict].message")
                images.extend(imgs)
            
            # 直接检查是否是图片段
            if obj.get('type') == 'image' and 'data' in obj:
                url = obj['data'].get('url') or obj['data'].get('file')
                if url:
                    logger.info(f"[_ExtractObj] {source} 本身是图片段: {url[:50]}...")
                    images.append(url)
    except Exception as e:
        logger.debug(f"[_ExtractObj] 提取失败: {e}")
    
    return images


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