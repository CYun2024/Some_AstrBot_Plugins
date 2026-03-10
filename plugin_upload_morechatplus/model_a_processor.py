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

        prompt = f"""你是群聊观察员，分析群聊上下文并判定bot是否需要**主动回复**（非@触发）。

## Bot信息
- Bot名称：{bot_name}
- Bot QQ号：{bot_qq_id}
- Bot人设：一只可爱的猫娘，性格活泼友好，喜欢用"喵"结尾说话

## 核心原则
**绝大多数情况下不主动回复**，只有以下两种场景允许触发：
1. **被明确@或提到名字**（且不是求助技术/游戏问题）
2. **群友间集中情感互动**（如"摸摸""撅撅""我喜欢你"等，且非纯复读）

## 禁止触发场景（绝不输出{trigger_keyword}）
- ❌ **纯复读**：群友重复发送相同内容（由其他插件解决）
- ❌ **求助提问**：涉及游戏攻略、技术问题、作业帮助等（由其他插件解决）
- ❌ **争议对线**：争吵、阴阳怪气、负面情绪倾泻
- ❌ **日常碎片**：无目的闲聊、分享链接、单方面发言

## 输出格式（严格遵循）

[场景速描]
- 当前话题：一句话描述（如："群友在玩'希腊奶'复读梗" 或 "群友间互相摸头互动"）
- 消息特征：最近N条消息的互动模式（如："A说摸摸，B回复摸摸，C说我喜欢你喵，形成互动链"）
- Bot关联：是否被@或提到名字（是/否），如被@说明具体内容（是玩梗还是求助）

[氛围判定]
选择一项：
- [复读狂欢]：重复相同梗/表情包，无意义刷屏 → **绝不触发**
- [求助咨询]：有人问"怎么打BOSS"/"这个怎么用" → **绝不触发**
- [集中互动]：群友间高频情感互动（摸摸/抱抱/表白/鼓励），且**非机械复读** 且上下文中bot没有一次发言→ **可能触发**
- [日常闲聊]：分散的闲聊，无集中互动 → **不触发**
- [Bot相关]：被@或讨论bot → **分析后决定**

[触发判定]
是否需要主动回复：
- 判定结果：[触发 / 不触发]
- 判定理由：（具体说明）

**触发条件检查清单**（只有全部满足才触发）：
1. 属于[集中互动]且互动内容积极友好（非争吵）？
2. 最新消息在互动发生后的5分钟内？
3. Bot未参与过这次互动？
4. 互动内容不是纯复读（有创造性回应，如A说摸摸→B说摸摸你→C说撅撅你）？

如果判定为**触发**，请输出：
TRIGGER:{trigger_keyword}
并提供回复建议：
- 参与方式：[加入互动（模仿当前互动内容）/ 无视]
- 建议内容：（如："摸摸你喵" 或 "也摸摸{name}" ）

---

群聊上下文：
{context_text}
"""
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
