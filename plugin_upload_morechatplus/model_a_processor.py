"""模型A处理器 - 上下文总结与主动回复判定（支持备用模型）"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.provider import Provider

from .context_manager import ContextManager
from .database import DatabaseManager
from .plugin_config import PluginConfig
from .model_utils import call_model_with_fallback, ModelCallResult


@dataclass
class SummaryResult:
    """总结结果（结构化）"""
    summary: str = ""  # 原始总结文本
    topic_analysis: str = ""  # 话题分析
    suggestions: str = ""  # 建议
    should_reply: bool = False
    reply_target_msg_id: str = ""
    reply_suggestion: str = ""
    used_fallback: bool = False
    provider_id: str = ""

    # 新增结构化字段
    timestamp: float = 0.0
    active_topic: str = ""  # 当前活跃话题
    topic_duration: str = ""  # 持续时间
    topic_evolution: List[str] = field(default_factory=list)  # 话题演变过程
    participants: List[Dict] = field(default_factory=list)  # 参与成员列表


class ModelAProcessor:
    """模型A处理器"""

    def __init__(
        self,
        db: DatabaseManager,
        context_manager: ContextManager,
        config: PluginConfig,
        context,
        debugger=None,
    ):
        self.db = db
        self.context_manager = context_manager
        self.config = config
        self.context = context
        self.debugger = debugger

    async def _record_llm_call(self, data: dict):
        """辅助方法：安全上报到LLM Debugger"""
        try:
            if not hasattr(self.context, '_plugin_instances'):
                logger.debug("[MoreChatPlus] context 没有 _plugin_instances 属性")
                return

            debugger = self.context._plugin_instances.get('llm_debugger')
            if not debugger:
                logger.debug("[MoreChatPlus] LLM Debugger 实例未找到")
                return

            if not hasattr(debugger, 'record_llm_call'):
                logger.debug("[MoreChatPlus] LLM Debugger 没有 record_llm_call 方法")
                return

            if "timestamp" not in data:
                data["timestamp"] = time.time()
            if "source" not in data:
                data["source"] = {"plugin": "morechatplus", "purpose": "unknown"}
            if "conversation_id" not in data:
                data["conversation_id"] = uuid.uuid4().hex

            await debugger.record_llm_call(data)
            logger.debug(f"[MoreChatPlus] 成功上报LLM调用: {data.get('phase')}")

        except Exception as e:
            logger.error(f"[MoreChatPlus] 上报LLM调用失败: {e}")

    async def process_context(self, origin: str) -> Optional[SummaryResult]:
        """处理上下文，生成总结和回复建议"""
        try:
            context_text = self.context_manager.get_context_for_model_a(origin)
            prompt = self._build_summary_prompt(context_text)

            primary_id = self.config.models.model_a_provider
            fallback_id = self.config.models.model_a_fallback_provider

            logger.info(
                f"[MoreChatPlus] 调用模型A | origin={origin} | "
                f"主模型={primary_id or 'default'} | 备用={fallback_id or '无'}"
            )

            result = await call_model_with_fallback(
                context=self.context,
                config=self.config,
                primary_provider_id=primary_id,
                fallback_provider_id=fallback_id,
                prompt=prompt,
                timeout_sec=self.config.timeouts.model_a_sec,
                record_callback=self._record_llm_call,
                purpose="model_a_summary"
            )

            if not result.success:
                logger.error(f"[MoreChatPlus] 模型A调用失败: {result.error}")
                return None

            if result.is_fallback:
                logger.info(f"[MoreChatPlus] 模型A已切换到备用模型: {result.provider_id}")

            parsed = self._parse_summary_response(result.text, result.provider_id, result.is_fallback)

            if parsed:
                messages = self.db.get_messages(origin, limit=self.config.context.summary_interval)
                if len(messages) >= 2:
                    start_msg_id = messages[-1].message_id
                    end_msg_id = messages[0].message_id

                    self.db.save_summary(
                        origin=origin,
                        start_msg_id=start_msg_id,
                        end_msg_id=end_msg_id,
                        summary=parsed.summary,
                        topic_analysis=parsed.topic_analysis,
                        suggestions=parsed.suggestions,
                        should_reply=parsed.should_reply,
                    )

                logger.info(
                    f"[MoreChatPlus] 模型A总结完成 | origin={origin} "
                    f"should_reply={parsed.should_reply}"
                )

            return parsed

        except Exception as e:
            logger.error(f"[MoreChatPlus] 模型A处理失败: {e}")
            return None

    def _build_summary_prompt(self, context_text: str) -> str:
        """构建总结提示词（结构化输出）"""
        trigger_keyword = self.config.active_reply.trigger_keyword
        bot_name = self.config.core.bot_name
        bot_qq_id = self.config.core.bot_qq_id
        current_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())

        prompt = f"""你是群聊话题分析专家。请分析以下群聊上下文，提取关键信息并以结构化JSON格式输出。

当前时间：{current_time}
Bot信息：
- 名称：{bot_name}
- QQ：{bot_qq_id}
- 人设：活泼猫娘，爱说"喵"

## 分析任务

1. **识别当前活跃话题**（15字以内概括）
2. **追踪话题演变**：从最早到最近，话题是如何发展的（如：GPU选型 → vLLM部署 → 内存碎片问题）
3. **识别核心参与成员**：找出3-5个主要发言者，分析他们的角色特征
   - 用户ID（必须准确）
   - 角色定位（如：资深架构师/算法工程师/实习生）
   - 发言风格（如：说话简洁/关注细节/常提问）
   - 当前关注点
4. **判定是否需要Bot主动回复**

## 输出格式（必须严格遵循JSON格式）

```json
{{
  "timestamp": "{current_time}",
  "active_topic": "当前话题名称（15字内）",
  "topic_duration": "已持续X分钟",
  "topic_evolution": ["话题阶段1", "话题阶段2", "当前阶段"],
  "participants": [
    {{
      "user_id": "QQ号",
      "nickname": "当前昵称",
      "role": "角色定位",
      "style": "发言风格",
      "focus": "当前关注点"
    }}
  ],
  "bot_should_reply": false,
  "reply_reason": "如果需要回复，说明原因",
  "reply_suggestion": "建议回复内容（如果should_reply为true）"
}}
```

## 触发规则（仅在以下情况设置bot_should_reply为true）

1. 被@且不是问问题（如@bot玩梗、@bot卖萌）
2. 群友间密集情感互动（互相摸/撅/表白，且bot没参与过）

## 绝不触发（看到就设置false）
- 复读刷屏（如连续"希腊奶"）
- 有人问攻略/技术问题 → 不触发  
- 吵架/阴阳怪气 → 不触发
- 各说各话的日常闲聊 → 不触发

---

群聊上下文：
{context_text}

请直接输出JSON，不要有任何其他文字说明。"""
        return prompt

    def _parse_summary_response(self, text: str, provider_id: str = "", is_fallback: bool = False) -> Optional[SummaryResult]:
        """解析总结响应（适配结构化JSON）"""
        try:
            # 提取JSON部分
            json_match = re.search(r'```json\s*\n?(.*?)\n?```', text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 尝试直接找JSON对象
                json_match = re.search(r'\{[\s\S]*\}', text)
                if json_match:
                    json_str = json_match.group(0)
                else:
                    json_str = text

            data = json.loads(json_str)

            # 构建SummaryResult
            result = SummaryResult(
                summary=f"活跃话题：{data.get('active_topic', '无')}",
                topic_analysis=f"话题演变：{' → '.join(data.get('topic_evolution', []))}",
                suggestions=data.get('reply_reason', ''),
                should_reply=data.get('bot_should_reply', False),
                reply_suggestion=data.get('reply_suggestion', ''),
                used_fallback=is_fallback,
                provider_id=provider_id,
                timestamp=time.time(),
                active_topic=data.get('active_topic', ''),
                topic_duration=data.get('topic_duration', ''),
                topic_evolution=data.get('topic_evolution', []),
                participants=data.get('participants', [])
            )

            # 如果明确有建议的回复目标，尝试提取
            if result.should_reply and not result.reply_target_msg_id:
                # 从上下文找最后发言者（非bot）
                result.reply_target_msg_id = ""

            return result

        except json.JSONDecodeError as e:
            logger.error(f"[MoreChatPlus] 解析模型A JSON响应失败: {e}, 响应: {text[:200]}")
            # 回退到旧解析逻辑
            return self._parse_summary_response_legacy(text, provider_id, is_fallback)
        except Exception as e:
            logger.error(f"[MoreChatPlus] 解析总结响应失败: {e}")
            return None

    def _parse_summary_response_legacy(self, text: str, provider_id: str = "", is_fallback: bool = False) -> Optional[SummaryResult]:
        """旧版解析逻辑（兼容）"""
        try:
            # 提取场景速描
            summary_match = re.search(
                r'\[场景速描\]\s*\n?(.*?)(?=\[氛围判定\]|\[触发判定\]|$)',
                text, re.DOTALL
            )
            summary = summary_match.group(1).strip() if summary_match else ""

            # 提取氛围判定
            atmosphere_match = re.search(
                r'\[氛围判定\]\s*\n?(.*?)(?=\[触发判定\]|$)',
                text, re.DOTALL
            )
            atmosphere = atmosphere_match.group(1).strip() if atmosphere_match else ""

            # 提取触发判定部分
            trigger_match = re.search(
                r'\[触发判定\]\s*\n?(.*)',
                text, re.DOTALL
            )
            trigger_section = trigger_match.group(1).strip() if trigger_match else ""

            # 组合topic_analysis
            topic_analysis = f"氛围：{atmosphere}\n判定：{trigger_section}"

            # 判断是否触发（严格检查）
            trigger_keyword = self.config.active_reply.trigger_keyword
            should_reply = (
                f"TRIGGER:{trigger_keyword}" in text or
                (trigger_keyword in text and "不触发" not in trigger_section)
            )

            # 如果明确写了"不触发"，强制设为False（防止误判）
            if "判定结果：[不触发]" in trigger_section or "判定结果：不触发" in trigger_section:
                should_reply = False

            # 提取回复建议（仅在触发时）
            reply_target_msg_id = ""
            reply_suggestion = ""
            if should_reply:
                # 尝试提取建议内容
                content_match = re.search(
                    r'建议内容[：:]\s*(.*?)(?:\n|$)',
                    text, re.DOTALL | re.IGNORECASE
                )
                if content_match:
                    reply_suggestion = content_match.group(1).strip()

            return SummaryResult(
                summary=summary,
                topic_analysis=topic_analysis,
                suggestions=trigger_section,
                should_reply=should_reply,
                reply_target_msg_id=reply_target_msg_id,
                reply_suggestion=reply_suggestion,
                used_fallback=is_fallback,
                provider_id=provider_id,
                timestamp=time.time()
            )

        except Exception as e:
            logger.error(f"[MoreChatPlus] 旧版解析失败: {e}")
            return None

    def format_summary_for_display(self, result: SummaryResult) -> str:
        """将总结结果格式化为【最近群聊话题】格式"""
        if not result:
            return "暂无话题总结"

        time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(result.timestamp)) if result.timestamp else "未知时间"

        lines = [
            f"- 时间：{time_str}",
        ]

        if result.active_topic:
            duration = result.topic_duration or ""
            lines.append(f"- 当前活跃话题：{result.active_topic}（{duration}）" if duration else f"- 当前活跃话题：{result.active_topic}")

        if result.topic_evolution:
            evolution_str = " → ".join(result.topic_evolution)
            lines.append(f"- 话题演变：{evolution_str}（当前）")

        if result.participants:
            lines.append("- 参与成员：")
            for i, p in enumerate(result.participants[:5], 1):  # 最多显示5人
                uid = p.get('user_id', '未知')
                role = p.get('role', '')
                style = p.get('style', '')
                nickname = p.get('nickname', '')
                desc = f"{role}，{style}" if role and style else (role or style or "群友")
                lines.append(f"  {i}. @{nickname}(uid:{uid}): {desc}")

        return "\n".join(lines)

    async def check_nickname_reference(
        self,
        nickname: str,
        origin: str,
        message_groups: List[List[Dict]],
    ) -> Optional[str]:
        """检查新昵称是否指向某个用户"""
        try:
            groups_text = []
            for i, group in enumerate(message_groups[:5], 1):
                group_texts = []
                for msg in group:
                    from datetime import datetime
                    time_str = datetime.fromtimestamp(
                        msg.timestamp
                    ).strftime("%H:%M:%S")
                    group_texts.append(
                        f"[{msg.nickname}|{msg.user_id}|{time_str}]: {msg.content}"
                    )
                groups_text.append(f"组{i}:\n" + "\n".join(group_texts))

            all_groups = "\n\n".join(groups_text)

            prompt = f"""分析以下群聊消息，判断昵称"{nickname}"最可能指向哪个用户。

消息记录：
{all_groups}

请分析：
1. 昵称"{nickname}"在每条消息中的上下文
2. 谁最可能被这样称呼
3. 是否有明确的指向关系

请只输出最可能的用户ID，如果不确定则输出"不确定"。

输出格式：
最可能用户ID: (用户ID或"不确定")
理由: (简要说明)
"""

            primary_id = self.config.models.model_a_provider
            fallback_id = self.config.models.model_a_fallback_provider

            result = await call_model_with_fallback(
                context=self.context,
                config=self.config,
                primary_provider_id=primary_id,
                fallback_provider_id=fallback_id,
                prompt=prompt,
                timeout_sec=self.config.timeouts.model_a_sec,
                record_callback=self._record_llm_call,
                purpose="nickname_check"
            )

            if not result.success:
                return None

            text = result.text
            match = re.search(r'最可能用户ID[：:]\s*(\d+)', text)
            if match:
                user_id = match.group(1)
                if user_id and user_id != "不确定":
                    return user_id

            return None

        except Exception as e:
            logger.error(f"[MoreChatPlus] 检查昵称引用失败: {e}")
            return None

    async def detect_new_nickname_in_summary(
        self,
        origin: str,
    ) -> List[Tuple[str, str]]:
        """在总结时检测新昵称"""
        messages = self.db.get_messages(origin, limit=50)

        potential_names = []
        name_pattern = re.compile(r'我是([\u4e00-\u9fa5]{2,4})|叫我([\u4e00-\u9fa5]{2,4})|昵称是([\u4e00-\u9fa5]{2,4})')

        for msg in messages:
            matches = name_pattern.findall(msg.content)
            for match in matches:
                for name in match:
                    if name:
                        potential_names.append((name, msg.user_id))

        return potential_names