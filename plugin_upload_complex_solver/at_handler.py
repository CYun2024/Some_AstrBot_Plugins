"""
@提及功能处理模块
参考 at 插件实现，让模型知道是谁问的问题，并在输出时@那个人
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
    
    def build_at_instruction(self, event: AstrMessageEvent) -> str:
        """
        构建@提及的system prompt指令
        让模型知道当前提问者是谁，并在回复时@对方
        """
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        group_id = event.get_group_id()
        
        # 私聊不需要@
        if not group_id:
            return ""
        
        instruction = f'''
<at_mention_protocol>
    <description>回复时必须@提问者的协议</description>
    <current_context>
        <speaker>
            <name>{sender_name}</name>
            <id>{sender_id}</id>
        </speaker>
        <environment>{"群聊" if group_id else "私聊"}</environment>
    </current_context>
    <rules>
        <rule>当前说话者是：{sender_name} (QQ: {sender_id})</rule>
        <rule>回复时必须在开头使用 [at:{sender_id}] 来@提问者</rule>
        <rule>格式示例：[at:{sender_id}] 这是回复内容</rule>
        <rule>如果是群聊环境，必须@；如果是私聊，不需要@</rule>
    </rules>
    <examples>
        <example>
            <input>张三问：1+1等于几？</input>
            <output>[at:张三的QQ] 1+1等于2</output>
        </example>
        <example>
            <input>李四问：请解释一下这个公式</input>
            <output>[at:李四的QQ] 这个公式的含义是...</output>
        </example>
    </examples>
</at_mention_protocol>
'''
        return instruction
    
    def process_at_tags(self, text: str) -> List:
        """
        处理文本中的 [at:qq_number] 标签，转换为消息组件列表
        返回: [Plain, At, Plain, ...] 格式的列表
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
        
        # 如果没有找到任何@标签，返回原文本
        if not found_at:
            return [Plain(text)]
        
        return components
    
    def add_at_to_text(self, text: str, sender_id: str, sender_name: str, is_group: bool) -> str:
        """
        如果文本中没有@标签，在开头添加@提问者
        """
        if not is_group:
            return text
        
        # 检查是否已有@标签
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
