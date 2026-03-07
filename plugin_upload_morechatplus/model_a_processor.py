"""模型A处理器 - 上下文总结与主动回复判定（支持备用模型）"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.provider import Provider

from .context_manager import ContextManager
from .database import DatabaseManager
from .plugin_config import PluginConfig
from .model_utils import call_model_with_fallback, ModelCallResult


@dataclass
class SummaryResult:
    """总结结果"""
    summary: str
    topic_analysis: str
    suggestions: str
    should_reply: bool
    reply_target_msg_id: str = ""
    reply_suggestion: str = ""
    used_fallback: bool = False  # 新增：是否使用了备用模型
    provider_id: str = ""  # 新增：实际使用的模型ID


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
        self.debugger = debugger  # 可以是MoreChatPlusPlugin实例，它实现了safe_record_llm_call

    async def _record_llm_call(self, data: dict):
        """辅助方法：安全上报"""
        if self.debugger and hasattr(self.debugger, 'safe_record_llm_call'):
            await self.debugger.safe_record_llm_call(data)

    async def process_context(self, origin: str) -> Optional[SummaryResult]:
        """处理上下文，生成总结和回复建议（支持故障转移）"""
        try:
            context_text = self.context_manager.get_context_for_model_a(origin)
            prompt = self._build_summary_prompt(context_text)

            primary_id = self.config.models.model_a_provider
            fallback_id = self.config.models.model_a_fallback_provider

            logger.info(
                f"[MoreChatPlus] 调用模型A进行上下文总结 | origin={origin} | "
                f"主模型={primary_id or 'default'} | 备用={fallback_id or '无'}"
            )

            # 使用新的故障转移调用方法
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

            parsed = self._parse_summary_response(result.text)

            if parsed:
                # 记录使用的模型信息
                parsed.used_fallback = result.is_fallback
                parsed.provider_id = result.provider_id

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
                    f"should_reply={parsed.should_reply} "
                    f"provider={parsed.provider_id} "
                    f"fallback={parsed.used_fallback}"
                )

            return parsed

        except Exception as e:
            logger.error(f"[MoreChatPlus] 模型A处理失败: {e}")
            return None

    def _build_summary_prompt(self, context_text: str) -> str:
        """构建总结提示词"""
        trigger_keyword = self.config.active_reply.trigger_keyword
        strict_hint = "非常严格" if self.config.active_reply.strict_mode else "适度"
        avoid_controversial = "避免参与有争议的话题。" if self.config.active_reply.avoid_controversial else ""

        return f"""请分析以下群聊上下文，完成以下任务：

## 任务1：话题总结
简要总结当前讨论的话题走向（2-3句话）。

## 任务2：话题分析
分析：
1. 当前话题是否与bot自身有关（如提到bot的名字、@bot等）
2. 话题的性质（日常闲聊/求助/争议/其他）
3. 群友的互动状态

## 任务3：回复建议
判断bot是否应该主动回复。判定标准（{strict_hint}）：
1. 话题明确与bot有关（提到bot名字或@bot）
2. 群友在友好地互动，且bot长时间未参与
3. 群友在复读，且bot未参与过
4. 群友在伤心倾倒负面情绪，bot可以进行简单的安慰（比如摸摸互动"摸摸你喵"）

{avoid_controversial}

如果判定需要回复，请输出标记：{trigger_keyword}
同时提供回复建议：应该回复哪条消息（消息ID），建议回复什么内容。

## 输出格式
请严格按照以下格式输出：

[话题总结]
总结内容...

[话题分析]
分析内容...

[回复建议]
是否需要回复：是/否
如果需要回复，在此处输出：{trigger_keyword}
回复目标消息ID：（如 #msg123456）
回复建议内容：建议回复什么...

---

群聊上下文：
{context_text}
"""

    def _parse_summary_response(self, text: str) -> Optional[SummaryResult]:
        """解析总结响应"""
        try:
            summary_match = re.search(
                r'\[话题总结\]\s*\n?(.*?)(?=\[话题分析\]|\[回复建议\]|$)',
                text, re.DOTALL
            )
            summary = summary_match.group(1).strip() if summary_match else ""

            analysis_match = re.search(
                r'\[话题分析\]\s*\n?(.*?)(?=\[回复建议\]|$)',
                text, re.DOTALL
            )
            topic_analysis = analysis_match.group(1).strip() if analysis_match else ""

            suggestion_match = re.search(
                r'\[回复建议\]\s*\n?(.*)',
                text, re.DOTALL
            )
            suggestions = suggestion_match.group(1).strip() if suggestion_match else ""

            trigger_keyword = self.config.active_reply.trigger_keyword
            should_reply = trigger_keyword in text

            msg_id_match = re.search(
                r'回复目标消息ID[：:]\s*#?msg?(\d+)',
                text, re.IGNORECASE
            )
            reply_target_msg_id = msg_id_match.group(1) if msg_id_match else ""

            reply_suggestion_match = re.search(
                r'回复建议内容[：:]\s*(.*?)(?:\n|$)',
                text, re.DOTALL | re.IGNORECASE
            )
            reply_suggestion = reply_suggestion_match.group(1).strip() if reply_suggestion_match else ""

            return SummaryResult(
                summary=summary,
                topic_analysis=topic_analysis,
                suggestions=suggestions,
                should_reply=should_reply,
                reply_target_msg_id=reply_target_msg_id,
                reply_suggestion=reply_suggestion,
            )

        except Exception as e:
            logger.error(f"[MoreChatPlus] 解析总结响应失败: {e}")
            return None

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
                    time_str = __import__('datetime').datetime.fromtimestamp(
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