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
    used_fallback: bool = False
    provider_id: str = ""


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

            parsed = self._parse_summary_response(result.text)

            if parsed:
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
                    f"should_reply={parsed.should_reply}"
                )

            return parsed

        except Exception as e:
            logger.error(f"[MoreChatPlus] 模型A处理失败: {e}")
            return None

    def _build_summary_prompt(self, context_text: str) -> str:
        """构建总结提示词（严格触发逻辑）"""
        trigger_keyword = self.config.active_reply.trigger_keyword
        bot_name = self.config.core.bot_name
        bot_qq_id = self.config.core.bot_qq_id

        prompt = f"""你是群聊观察员，分析群聊并判定bot是否需要主动回复（非@触发）。

## 绝对禁止
以下词汇严禁出现在输出中：文化、现象、机制、逻辑、无意义、去语义化、符号化、狂欢、延续、沉浸、互演、诉求、过渡、阶段、分析、展开。


## 强制语言风格
- 使用**大白话**，像小学生写日记一样描述
- **一句话说完**，不要分点论述

## Bot信息
- 名称：{bot_name}
- QQ：{bot_qq_id}
- 人设：活泼猫娘，爱说"喵"

## 何时触发（只有这两种）
1. 被@且不是问问题（如@bot玩梗、@bot卖萌）
2. 群友间密集情感互动（互相摸/撅/表白，且bot没参与过）

## 绝不触发（看到就写"不触发"）
- 复读刷屏（如连续"希腊奶"）→ 不触发
- 有人问"怎么打BOSS"/"这题怎么做" → 不触发  
- 吵架/阴阳怪气 → 不触发
- 各说各话的日常闲聊 → 不触发

## 输出格式（严格遵循，不得改动）

[场景速描]
- 当前话题：（绝对客观的描述群友在讨论什么话题，不要评价）
- 消息特征：（**一句话**，描述谁@了谁，发了什么，如"A发摸摸，B回撅撅"）
- Bot关联：（是/否，如果被@要说清楚是求助还是玩梗）

[氛围判定]
（**只选一项**，复制方括号里的文字）：
- [复读狂欢] 群友重复发相同内容 → 写这个 **绝不触发回复**
- [求助咨询] 有人问攻略/技术问题 → 写这个 **绝不触发回复**
- [集中互动] 群友间密集情感互动（摸/撅/表白）且bot没说过话 → 写这个 **可以触发回复**
- [日常闲聊] 各说各的，或分享链接 → 写这个 **不触发回复**
- [Bot相关] 有人@bot或讨论bot → 写这个 **看具体情况**

[触发判定]
是否需要主动回复：
- 判定结果：[触发 / 不触发]
- 判定理由：（**一句话**，如"群友在互相摸头，bot没参与，可以回复"或"只是复读梗，不回复"）

**触发检查清单**（全部满足才输出 TRIGGER:{trigger_keyword}）：
1. 属于[集中互动]且内容友好？
2. bot最近没说过话？
3. 不是复读（有变化，如摸摸→摸摸你→使劲摸）？

如果判定为**触发**，必须输出：
TRIGGER:{trigger_keyword}
- 参与方式：[加入互动]
- 建议内容：（具体写一句回复，如"摸摸你喵"或"我也喜欢你喵"）

---

群聊上下文：
{context_text}"""
        return prompt

    def _parse_summary_response(self, text: str) -> Optional[SummaryResult]:
        """解析总结响应（适配严格触发逻辑）"""
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

                # 尝试提取目标（从场景速描中找最后发言者）
                msg_match = re.search(r'msg[：:]?(\d+)', text)
                if msg_match:
                    reply_target_msg_id = msg_match.group(1)

            return SummaryResult(
                summary=summary,
                topic_analysis=topic_analysis,
                suggestions=trigger_section,
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