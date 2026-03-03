"""
@提及功能处理模块
"""
import re
from typing import Dict, Any, Optional, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, At


class AtHandler:
    """@提及处理器"""
    
    def __init__(self):
        # 匹配 [at:qq_number] 格式的标签
        self.valid_at_pattern = re.compile(r'\[at:(\d+)\]')
        # 匹配 [at:名字] 格式（非数字）用于检测错误用法
        self.invalid_at_pattern = re.compile(r'\[at:([^\]]+)\]')
    
    def build_at_instruction(self, event: AstrMessageEvent) -> str:
        """
        构建@提及的system prompt指令
        """
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        group_id = event.get_group_id()
        
        if not group_id:
            return ""
        
        instruction = f"""
<at_mention_protocol>
    <description>回复时必须@提问者的协议 - 严格遵守</description>
    <current_context>
        <speaker>
            <name>{sender_name}</name>
            <qq_number>{sender_id}</qq_number>
        </speaker>
        <environment>群聊</environment>
    </current_context>
    <rules>
        <rule>当前说话者是：{sender_name}</rule>
        <rule>【重要】必须在回复开头使用 [at:{sender_id}] 来@提问者</rule>
        <rule>【格式】严格使用：[at:数字QQ号] 例如：[at:123456789]</rule>
        <rule>【禁止】不能使用 [at:名字]，必须使用QQ数字号码</rule>
        <rule>【示例】[at:{sender_id}] 这是回复内容</rule>
        <rule>如果是私聊环境，不需要@</rule>
    </rules>
    <examples>
        <example>
            <input>张三(123456)问：1+1等于几？</input>
            <correct>[at:123456] 1+1等于2</correct>
            <wrong>[at:张三] 1+1等于2</wrong>
        </example>
    </examples>
</at_mention_protocol>
"""
        return instruction
    
    def process_at_tags(self, text: str) -> List:
        """
        处理文本中的 [at:qq_number] 标签，转换为消息组件列表
        """
        components = []
        last_idx = 0
        found_at = False
        
        for match in self.valid_at_pattern.finditer(text):
            start, end = match.span()
            found_at = True
            
            # 添加标签前的文本
            if start > last_idx:
                before_text = text[last_idx:start]
                if before_text:
                    components.append(Plain(before_text))
            
            # 添加@组件
            target_id = match.group(1)
            components.append(At(qq=target_id))
            
            last_idx = end
        
        # 添加剩余文本
        if last_idx < len(text):
            remaining = text[last_idx:]
            if remaining:
                components.append(Plain(remaining))
        
        if not found_at:
            return [Plain(text)]
        
        return components
    
    def fix_at_format(self, text: str, sender_id: str, sender_name: str) -> str:
        """
        修复LLM错误的@格式
        如果LLM使用了 [at:名字] 而不是 [at:QQ号]，进行修复
        """
        if not text:
            return text
            
        # 检查是否有错误的 [at:XXX] 格式（XXX不是纯数字）
        invalid_matches = list(self.invalid_at_pattern.finditer(text))
        
        for match in invalid_matches:
            at_content = match.group(1)
            # 如果不是纯数字，说明是名字而不是QQ号
            if not at_content.isdigit():
                logger.warning(f"[AtHandler] 检测到错误的@格式: {match.group(0)}，内容'{at_content}'不是数字")
                # 如果匹配到的是当前发送者的名字，替换为QQ号
                if at_content == sender_name or sender_name in at_content:
                    logger.info(f"[AtHandler] 修复@格式: {match.group(0)} -> [at:{sender_id}]")
                    text = text.replace(match.group(0), f"[at:{sender_id}]")
        
        return text
    
    def add_at_to_text(self, text: str, sender_id: str, sender_name: str, is_group: bool) -> str:
        """
        如果文本中没有@标签，在开头添加@提问者
        """
        if not is_group:
            return text
        
        # 先修复错误的格式
        text = self.fix_at_format(text, sender_id, sender_name)
        
        # 检查是否已有正确的@标签
        if self.valid_at_pattern.search(text):
            return text
        
        # 在开头添加@
        return f"[at:{sender_id}] {text}"
    
    def extract_sender_info(self, event: AstrMessageEvent) -> Dict[str, Any]:
        """提取发送者信息"""
        return {
            "id": event.get_sender_id(),
            "name": event.get_sender_name(),
            "group_id": event.get_group_id(),
            "platform": event.get_platform_name()
        }