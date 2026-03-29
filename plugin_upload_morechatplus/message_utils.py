"""消息处理工具模块"""

import re
from typing import List, Tuple

from astrbot.api.message_components import At, Plain, Reply


# 引用标签正则
QUOTE_RE = re.compile(
    r'<引用\s*[:：]?\s*#?msg?(\d+)\s*/?>',
    re.IGNORECASE
)
QUOTE_TAG_RE = re.compile(
    r'<引用[^>]*>.*?</引用>|<引用[^/]*/>',
    re.IGNORECASE | re.DOTALL
)
# 匹配 [引用:msgID] 格式的引用
QUOTE_REF_RE = re.compile(
    r'\[引用\s*[:：]?\s*#?(\d+)\]',
    re.IGNORECASE
)

# At标签正则
AT_RE = re.compile(
    r'\[at\s*[:：]?\s*(\d+)\]',
    re.IGNORECASE
)

# Image标签正则
IMAGE_RE = re.compile(
    r'\[image\s*[:：]?\s*([^\]]+)\]',
    re.IGNORECASE
)

# 其他需要清理的标签
CLEANUP_PATTERNS = [
    re.compile(r'<[^>]+>', re.DOTALL),  # 所有HTML/XML标签
    re.compile(r'\[引用[^\]]*\]', re.IGNORECASE),
]


def clean_message_for_sending(text: str) -> Tuple[str, str, List[str]]:
    """清理消息内容，准备发送

    强化清理引用标签和其他残留标签

    Returns:
        (清理后的文本, 引用ID, at的QQ号列表)
    """
    if not text:
        return text, None, []

    original = text

    # 第一步：处理引用标签（保留ID用于后续处理）
    quote_id = None
    quote_match = QUOTE_RE.search(text)
    if quote_match:
        quote_id = quote_match.group(1)

    # 移除所有引用标签变体
    text = QUOTE_RE.sub('', text)
    text = QUOTE_TAG_RE.sub('', text)

    # 第二步：处理at标签（保留用于后续处理）
    at_ids = AT_RE.findall(text)

    # 第三步：处理image标签
    text = IMAGE_RE.sub('[图片]', text)

    # 第四步：清理其他标签
    for pattern in CLEANUP_PATTERNS:
        text = pattern.sub('', text)

    # 第五步：清理残留的特殊字符
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)  # 控制字符

    # 第六步：规范化空白
    text = re.sub(r'\n+', '\n', text)  # 多个换行合并
    text = re.sub(r' +', ' ', text)    # 多个空格合并
    text = text.strip()

    # 记录清理结果
    if text != original:
        import logging
        logging.getLogger(__name__).debug(
            f"[MoreChatPlus] 消息清理: '{original[:50]}...' -> '{text[:50]}...'"
        )

    return text, quote_id, at_ids


def parse_at_tags(text: str) -> Tuple[str, List[str]]:
    """解析文本中的at标签

    Returns:
        (清理后的文本, at的QQ号列表)
    """
    at_ids = AT_RE.findall(text)
    cleaned = AT_RE.sub('', text)
    cleaned = re.sub(r' +', ' ', cleaned).strip()
    return cleaned, at_ids


def build_message_chain(
    text: str,
    quote_id: str = None,
    at_ids: List[str] = None,
) -> List:
    """构建消息链

    Args:
        text: 消息文本
        quote_id: 引用的消息ID
        at_ids: 要@的用户ID列表

    Returns:
        消息组件列表
    """
    chain = []

    # 添加引用
    if quote_id:
        chain.append(Reply(id=quote_id))

    # 添加at
    if at_ids:
        for at_id in at_ids:
            if at_id:
                chain.append(At(qq=at_id))
                chain.append(Plain(" "))

    # 添加文本
    if text:
        chain.append(Plain(text))

    return chain


def format_context_message(
    nickname: str,
    user_id: str,
    timestamp: float,
    message_id: str,
    content: str,
    is_admin: bool = False,
    reply_to: str = None,
) -> str:
    """格式化上下文消息（统一格式与其他模块一致）

    格式: [虹猫猫|28196593|19:20:05]:(msg:267518526)[管理员] <引用:977370735> [at:机巧猫] [image:2384390259023809] 可爱喵~

    注意：与其他文件格式保持一致，使用 msg: 前缀而非 #msg
    """
    from datetime import datetime
    time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")

    admin_mark = "[管理员]" if is_admin else ""

    # 统一格式：[昵称|user_id|时间]:(msg:消息ID)[管理员标记]
    header = f"[{nickname}|{user_id}|{time_str}]:(msg:{message_id}){admin_mark}"

    # 使用 <引用:msgID> 格式，不包含引用内容
    reply_part = ""
    if reply_to:
        reply_part = f" <引用:{reply_to}>"

    return f"{header}{reply_part} {content}"


def extract_message_info_from_text(text: str) -> dict:
    """从格式化的消息文本中提取信息"""
    # 更新正则以匹配新格式 [昵称|user_id|时间]:(msg:消息ID)
    pattern = r'\[([^|]+)\|(\d+)\|(\d{2}:\d{2}:\d{2})\]:\(msg:(\d+)\)'
    match = re.match(pattern, text)

    if match:
        return {
            "nickname": match.group(1),
            "user_id": match.group(2),
            "time": match.group(3),
            "message_id": match.group(4),
        }
    return None


def should_trigger_reply(
    text: str,
    bot_name: str,
    bot_qq_id: str,
    trigger_words: List[str],
) -> bool:
    """检查是否应该触发回复"""
    text_lower = text.lower()

    # 检查@bot
    if bot_qq_id and f"[at:{bot_qq_id}]" in text:
        return True

    # 检查bot名字
    if bot_name and bot_name.lower() in text_lower:
        return True

    # 检查触发词
    for word in trigger_words:
        if word.lower() in text_lower:
            return True

    return False


def final_cleanup_chain(chain: List) -> List:
    """最终清理消息链，确保没有残留标签，并将[at:QQ号]转换为At组件"""
    if not chain:
        return chain

    result = []
    for comp in chain:
        if isinstance(comp, Plain):
            text = comp.text

            # 强化清理：移除所有可能的引用标签变体（更严格的正则）
            # 匹配 <引用:msg123> 或 <引用 #msg123> 等各种变体
            text = re.sub(r'<\s*引用\s*[:：]?\s*#?msg?\d+\s*/?\s*>', '', text, flags=re.IGNORECASE)
            # 匹配 <引用 ...>...</引用> 多行情况
            text = re.sub(r'<\s*引用\s*[^>]*>.*?<\s*/\s*引用\s*>', '', text, flags=re.IGNORECASE | re.DOTALL)
            # 匹配自闭合标签 <引用 ... />
            text = re.sub(r'<\s*引用\s*[^/]*/\s*>', '', text, flags=re.IGNORECASE)
            # 匹配所有包含"引用"字样的尖括号内容（最严格的兜底）
            text = re.sub(r'<\s*[^>]*引用[^>]*\s*/?\s*>', '', text, flags=re.IGNORECASE)

            # 清理其他残留标签（HTML/XML）
            text = re.sub(r'<[^>]+>', '', text)

            # 清理控制字符和特殊空白
            text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', text)
            text = re.sub(r'\n+', '\n', text)
            text = re.sub(r' +', ' ', text)
            text = text.strip()

            # 处理 [at:QQ号] 标签 - 转换为 At 组件
            if text:
                # 查找所有 [at:QQ号] 的位置
                parts = []
                last_end = 0
                for match in AT_RE.finditer(text):
                    # 添加匹配前的文本
                    if match.start() > last_end:
                        before_text = text[last_end:match.start()].strip()
                        if before_text:
                            parts.append(Plain(before_text))
                    # 添加 At 组件
                    qq = match.group(1)
                    if qq:
                        parts.append(At(qq=qq))
                    last_end = match.end()

                # 添加剩余文本
                if last_end < len(text):
                    remaining = text[last_end:].strip()
                    if remaining:
                        parts.append(Plain(remaining))

                # 如果没有匹配到 at 标签，直接添加原文本
                if not parts:
                    result.append(Plain(text))
                else:
                    result.extend(parts)
        else:
            result.append(comp)

    return result


def convert_at_tags_to_components(text: str) -> List:
    """将文本中的 [at:QQ号] 标签转换为消息组件列表

    Args:
        text: 包含 [at:QQ号] 标签的文本

    Returns:
        消息组件列表（At, Plain等）
    """
    if not text:
        return []

    chain = []
    last_end = 0

    for match in AT_RE.finditer(text):
        # 添加匹配前的文本
        if match.start() > last_end:
            before_text = text[last_end:match.start()].strip()
            if before_text:
                chain.append(Plain(before_text))

        # 添加 At 组件
        qq = match.group(1)
        if qq:
            chain.append(At(qq=qq))

        last_end = match.end()

    # 添加剩余文本
    if last_end < len(text):
        remaining = text[last_end:].strip()
        if remaining:
            chain.append(Plain(remaining))

    # 如果没有匹配到任何内容，返回原文本
    if not chain:
        return [Plain(text)]

    return chain


def parse_reply_content(content: str) -> Tuple[str, str, str]:
    """解析消息内容，分离引用信息和实际内容

    格式: [引用:977370735] 引用内容 [at:xxx] 实际回复内容

    Returns:
        (引用ID, 引用内容, 实际回复内容)
    """
    reply_id = ""
    reply_content = ""
    actual_content = content

    # 查找 [引用:ID] 格式
    match = QUOTE_REF_RE.search(content)
    if match:
        reply_id = match.group(1)
        # 获取引用标记后的内容
        after_quote = content[match.end():].strip()

        # 查找下一个 [at: 或 [image: 作为实际内容的开始
        next_tag_match = re.search(r'\[(at|image):', after_quote, re.IGNORECASE)
        if next_tag_match:
            reply_content = after_quote[:next_tag_match.start()].strip()
            actual_content = after_quote[next_tag_match.start():].strip()
        else:
            # 如果没有找到标签，假设引用内容为空，全部作为实际内容
            actual_content = after_quote
            reply_content = ""

    return reply_id, reply_content, actual_content