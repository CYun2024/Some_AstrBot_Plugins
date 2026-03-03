"""
人设复述模块
处理人设获取和复述相关功能
"""
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter
from .utils import truncate_text


class PersonaHandler:
    """人设处理器"""

    def __init__(self, context: Context, debugger: DebuggerReporter):
        self.context = context
        self.debugger = debugger

    async def get_persona_prompt(self) -> str:
        """获取当前人设提示词"""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
            if isinstance(persona, dict):
                return persona.get('prompt', "一只猫娘助手")
            else:
                return getattr(persona, 'prompt', "一只猫娘助手") if persona else "一只猫娘助手"
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}，使用默认")
            return "一只猫娘助手"

    async def restate_with_persona(
        self,
        provider_id: str,
        text: str,
        original_question: str,
        sender_info: dict,
        conv_id: str
    ) -> str:
        """
        使用人设复述答案
        让主LLM用人设重新表述专业模型的解答
        """
        persona_prompt = await self.get_persona_prompt()

        prompt = f"""这是你的人设:{persona_prompt}。现在你需要用你的角色风格重新表述下面专业模型给出的解答。要求：
1. 直接一模一样复述答案，在语句末尾加上简单的口癖（比如喵~）也是可行的。你已经获得了回答，请不要说你不会。如果你感觉解答是乱码也请直接一字不落的复述。
2. 如果你能力较强，可以转述回答，严格保留解答的逻辑、步骤和正确性，不得修改任何数学公式、推理步骤。
3. 用你的角色口吻重新组织语言，可以添加符合角色设定的语气词、表情符号等。
4. 如果解答中包含LaTeX数学公式，请保留原样（例如$...$或$$...$$），不要修改。

用户问题：{original_question}

专业模型解答：
{text}

"""

        # 上报请求
        await self.debugger.report_request(
            provider_id=provider_id,
            model="unknown",
            prompt=prompt,
            images=[],
            purpose="persona_restate",
            sender_info=sender_info,
            conv_id=conv_id,
            system_prompt="",
            contexts=[]
        )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text

            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                response=result,
                purpose="persona_restate",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )

            return result
        except Exception as e:
            logger.error(f"人设复述失败: {e}")
            return text

    async def generate_waiting_message(
        self,
        provider_id: str,
        round_num: int,
        question: str,
        sender_info: dict,
        conv_id: str
    ) -> str:
        """生成等待提示消息"""
        persona_prompt = await self.get_persona_prompt()

        prompt = f"""你是一个{persona_prompt}。用户提出了一个复杂问题，正在等待解题模型返回结果（已经等待了 {round_num * 3} 分钟）。
请用你的角色风格向用户说明正在努力思考中，可能需要再等待一段时间，语气要温和、有礼貌。不要回答原问题，而是表示抱歉。

请生成一段简短的等待提示（30字内）："""

        # 上报请求
        await self.debugger.report_request(
            provider_id=provider_id,
            model="unknown",
            prompt=prompt,
            images=[],
            purpose="waiting_message",
            sender_info=sender_info,
            conv_id=conv_id,
            system_prompt="",
            contexts=[]
        )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text.strip()

            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                response=result,
                purpose="waiting_message",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )

            return result
        except Exception as e:
            logger.error(f"生成等待消息失败: {e}")
            return f"[思考中...已等待{round_num * 3}分钟，请稍候，正在努力解决您的问题]"

    async def generate_apology(
        self,
        provider_id: str,
        reason: str,
        original_question: str,
        sender_info: dict,
        conv_id: str
    ) -> str:
        """生成道歉消息"""
        persona_prompt = await self.get_persona_prompt()

        prompt = f"""你是一个{persona_prompt}。用户刚才提出了一个复杂问题，但由于技术原因暂时无法解答。
请用你的角色风格向用户表达歉意，并说明原因（{reason}），不要尝试回答问题或提供解答。

用户问题：{original_question}

请用你的角色风格说一些抱歉的话："""

        # 上报请求
        await self.debugger.report_request(
            provider_id=provider_id,
            model="unknown",
            prompt=prompt,
            images=[],
            purpose="apology",
            sender_info=sender_info,
            conv_id=conv_id,
            system_prompt="",
            contexts=[]
        )

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            result = resp.completion_text

            # 上报响应
            await self.debugger.report_response(
                provider_id=provider_id,
                model=getattr(resp, 'model', 'unknown'),
                response=result,
                purpose="apology",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )

            return result
        except Exception as e:
            logger.error(f"生成道歉消息失败: {e}")
            return f"抱歉，{reason}"