"""模型A处理器 - 上下文总结与主动回复判定"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.provider import Provider

from .context_manager import ContextManager
from .database import DatabaseManager
from .plugin_config import PluginConfig


@dataclass
class SummaryResult:
    """总结结果"""
    summary: str
    topic_analysis: str
    suggestions: str
    should_reply: bool
    reply_target_msg_id: str = ""
    reply_suggestion: str = ""


class ModelAProcessor:
    """模型A处理器"""

    def __init__(
        self,
        db: DatabaseManager,
        context_manager: ContextManager,
        config: PluginConfig,
        context,
    ):
        self.db = db
        self.context_manager = context_manager
        self.config = config
        self.context = context

    async def process_context(self, origin: str) -> Optional[SummaryResult]:
        """处理上下文，生成总结和回复建议"""
        try:
            # 获取上下文
            context_text = self.context_manager.get_context_for_model_a(origin)

            # 构建提示词
            prompt = self._build_summary_prompt(context_text)

            # 调用模型A
            provider_id = self.config.models.model_a_provider
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider()

            if not provider:
                logger.warning("[MoreChatPlus] 模型A提供商不可用")
                return None

            logger.info(f"[MoreChatPlus] 调用模型A进行上下文总结 | origin={origin}")

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=uuid.uuid4().hex,
                    persist=False,
                ),
                timeout=self.config.timeouts.model_a_sec,
            )

            # 解析响应
            result = self._parse_summary_response(response.completion_text or "")

            # 保存总结到数据库
            if result:
                # 获取最近的消息ID范围
                messages = self.db.get_messages(origin, limit=self.config.context.summary_interval)
                if len(messages) >= 2:
                    start_msg_id = messages[-1].message_id
                    end_msg_id = messages[0].message_id

                    self.db.save_summary(
                        origin=origin,
                        start_msg_id=start_msg_id,
                        end_msg_id=end_msg_id,
                        summary=result.summary,
                        topic_analysis=result.topic_analysis,
                        suggestions=result.suggestions,
                        should_reply=result.should_reply,
                    )

                logger.info(
                    f"[MoreChatPlus] 模型A总结完成 | origin={origin} "
                    f"should_reply={result.should_reply}"
                )

            return result

        except asyncio.TimeoutError:
            logger.error("[MoreChatPlus] 模型A调用超时")
            return None
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
4. 群友在求助，bot可以提供帮助

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
            # 提取话题总结
            summary_match = re.search(
                r'\[话题总结\]\s*\n?(.*?)(?=\[话题分析\]|\[回复建议\]|$)',
                text, re.DOTALL
            )
            summary = summary_match.group(1).strip() if summary_match else ""

            # 提取话题分析
            analysis_match = re.search(
                r'\[话题分析\]\s*\n?(.*?)(?=\[回复建议\]|$)',
                text, re.DOTALL
            )
            topic_analysis = analysis_match.group(1).strip() if analysis_match else ""

            # 提取回复建议部分
            suggestion_match = re.search(
                r'\[回复建议\]\s*\n?(.*)',
                text, re.DOTALL
            )
            suggestions = suggestion_match.group(1).strip() if suggestion_match else ""

            # 判定是否需要回复
            trigger_keyword = self.config.active_reply.trigger_keyword
            should_reply = trigger_keyword in text

            # 提取回复目标消息ID
            msg_id_match = re.search(
                r'回复目标消息ID[：:]\s*#?msg?(\d+)',
                text, re.IGNORECASE
            )
            reply_target_msg_id = msg_id_match.group(1) if msg_id_match else ""

            # 提取回复建议内容
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
        """检查新昵称是否指向某个用户

        Args:
            nickname: 新出现的昵称
            origin: 消息来源
            message_groups: 消息组列表

        Returns:
            如果确定指向某个用户，返回user_id，否则None
        """
        try:
            # 构建消息组文本
            groups_text = []
            for i, group in enumerate(message_groups[:5], 1):  # 最多5组
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

            provider_id = self.config.models.model_a_provider
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider()

            if not provider:
                return None

            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=uuid.uuid4().hex,
                    persist=False,
                ),
                timeout=self.config.timeouts.model_a_sec,
            )

            text = response.completion_text or ""

            # 提取用户ID
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
        """在总结时检测新昵称

        Returns:
            List of (nickname, potential_user_id)
        """
        # 获取最近的消息
        messages = self.db.get_messages(origin, limit=50)

        # 提取可能的人名（简化处理，实际应该用NLP）
        potential_names = []
        name_pattern = re.compile(r'我是([\u4e00-\u9fa5]{2,4})|叫我([\u4e00-\u9fa5]{2,4})|昵称是([\u4e00-\u9fa5]{2,4})')

        for msg in messages:
            matches = name_pattern.findall(msg.content)
            for match in matches:
                for name in match:
                    if name:
                        potential_names.append((name, msg.user_id))

        return potential_names
