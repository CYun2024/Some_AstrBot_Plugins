"""
意图判断模块 - 使用快速LLM进行意图识别和质量判断
"""
import json
import re
from typing import List, Optional, Dict, Any

from astrbot.api import logger
from astrbot.api.star import Context

from .debugger_reporter import DebuggerReporter


class IntentJudge:
    """意图判断器 - 快速轻量模型"""

    def __init__(self, context: Context, debugger: DebuggerReporter, 
                 judge_provider: str, judge_model: str = ""):
        self.context = context
        self.debugger = debugger
        self.judge_provider = judge_provider
        # judge_model 保留参数但不再使用，仅向后兼容

    def is_configured(self) -> bool:
        """检查是否配置了判断模型"""
        return bool(self.judge_provider)

    async def classify_intent(
        self,
        user_text: str,
        has_images: bool,
        sender_info: dict,
        conv_id: str
    ) -> Dict[str, Any]:
        """
        判断用户意图 - 更严格的触发判定
        返回: {
            "intent": "ocr|scene|solver|chat",
            "confidence": float,
            "reason": str,
            "needs_vision": bool
        }
        """
        if not self.is_configured():
            logger.warning("[IntentJudge] 未配置判断模型，使用默认规则判断")
            return self._rule_based_intent(user_text, has_images)

        prompt = self._build_intent_prompt(user_text, has_images)

        logger.info(f"[IntentJudge] 开始判断意图: '{user_text[:50]}...'")
        logger.debug(f"[IntentJudge] 是否有图片: {has_images}")

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.judge_provider,
                prompt=prompt
            )
            
            result_text = resp.completion_text.strip()
            logger.debug(f"[IntentJudge] 判断模型原始输出: {result_text}")

            intent_data = self._parse_intent_result(result_text)
            
            logger.info(f"[IntentJudge] 意图判断结果: {intent_data['intent']} "
                       f"(置信度: {intent_data.get('confidence', 'N/A')}, "
                       f"原因: {intent_data.get('reason', 'N/A')})")

            # 上报到debugger
            await self.debugger.report_request(
                provider_id=self.judge_provider,
                model="default",
                prompt=prompt,
                images=[],
                purpose="intent_classification",
                sender_info=sender_info,
                conv_id=conv_id,
                system_prompt="你是一个意图分类助手，专门分析用户是否需要识别图片文字、理解场景，或只是聊天。",
                contexts=[]
            )

            await self.debugger.report_response(
                provider_id=self.judge_provider,
                model=getattr(resp, 'model', 'unknown'),
                response=json.dumps(intent_data, ensure_ascii=False),
                purpose="intent_classification",
                sender_info=sender_info,
                conv_id=conv_id,
                usage=getattr(resp, 'usage', None)
            )

            return intent_data

        except Exception as e:
            logger.error(f"[IntentJudge] 意图判断失败: {e}")
            return self._rule_based_intent(user_text, has_images)

    def _build_intent_prompt(self, user_text: str, has_images: bool) -> str:
        """构建意图判断提示词 - 严格触发版"""
        img_status = "用户发送了图片" if has_images else "用户未发送图片"
        
        prompt = f"""分析用户消息意图，决定是否触发复杂求解器。{img_status}

用户消息: "{user_text}"

【重要规则 - 严格模式】
1. **chat优先级最高（默认）**：除非有明确需求，否则一律判定为chat，不触发任何处理。

2. **必须同时满足以下条件才考虑非chat意图**：
   - 消息中包含明确的Bot唤醒词（"韶梦"/"shaomeng"）或@了Bot
   - 用户明确提出了具体需求（不是闲聊、不是语气词）

3. **solver触发条件（必须全部满足）**：
   - 明确提到"问题"、"题目"、"这道题"、"求解"、"怎么做"、"答案"、"计算"、"证明"
   - 且用户确实在询问解法（不是在闲聊中提到这些词）
   - 示例："韶梦，这道题怎么做" → solver

4. **ocr触发条件（必须全部满足）**：
   - 明确要求"识别文字"、"提取文字"、"写了什么"、"转文字"
   - 或明确提到"图片中的文字"、"截图里的内容"
   - 示例："韶梦，识别一下图片文字" → ocr

5. **scene触发条件（必须全部满足）**：
   - 明确要求"描述图片"、"图片里有什么"、"这是什么"
   - 且确实需要视觉理解（不是已经知道内容）
   - 示例："韶梦，这张图描述了什么" → scene；"这张图好美" → chat

6. **严格禁止的情况（一律chat）**：
   - 纯语气词（"哈哈"、"嗯嗯"、"哦"）
   - 简单问候（"你好"、"在吗"、"谢谢"）
   - 没有明确需求的感叹（"这道题好难"、"图片真好看"）
   - 未包含Bot唤醒词且未@Bot（除非私聊）

请以JSON格式输出（不要包含markdown代码块）：
{{
  "intent": "ocr|scene|solver|chat",
  "confidence": 0.0-1.0,
  "reason": "简短说明，提及检测到的关键词和判定依据",
  "needs_vision": true/false
}}

示例：
输入："回答一下这个问题韶梦" → {{"intent":"solver","confidence":0.9,"reason":"检测到solver关键词'回答'和'问题'，且包含唤醒词","needs_vision":false}}
输入："识别图片文字 @韶梦" → {{"intent":"ocr","confidence":0.95,"reason":"明确ocr指令且@了Bot","needs_vision":true}}
输入："哈哈哈" → {{"intent":"chat","confidence":0.95,"reason":"纯语气词，无具体需求","needs_vision":false}}
输入："这道题怎么做" → {{"intent":"chat","confidence":0.8,"reason":"虽有solver关键词但缺少Bot唤醒词，视为闲聊","needs_vision":false}}
输入："韶梦，这道题怎么做" → {{"intent":"solver","confidence":0.95,"reason":"solver关键词+唤醒词，明确求解意图","needs_vision":true}}"""
        
        return prompt

    def _parse_intent_result(self, text: str) -> Dict[str, Any]:
        """解析意图判断结果"""
        try:
            text = text.strip()
            # 去除可能的markdown代码块
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            
            data = json.loads(text)
            
            if "intent" not in data:
                raise ValueError("缺少intent字段")
            
            intent = data["intent"].lower()
            if intent not in ["ocr", "scene", "solver", "chat"]:
                logger.warning(f"[IntentJudge] 未知的意图类型: {intent}，默认设为chat")
                intent = "chat"
            
            return {
                "intent": intent,
                "confidence": float(data.get("confidence", 0.5)),
                "reason": data.get("reason", ""),
                "needs_vision": data.get("needs_vision", False) if intent != "chat" else False
            }
            
        except Exception as e:
            logger.error(f"[IntentJudge] 解析意图结果失败 '{text}': {e}")
            return self._fallback_intent_from_text(text)

    def _fallback_intent_from_text(self, text: str) -> Dict[str, Any]:
        """从文本中回退解析意图（当JSON解析失败时）"""
        text_lower = text.lower()
        
        if any(k in text_lower for k in ["ocr", "文字", "提取", "写了"]):
            return {"intent": "ocr", "confidence": 0.7, "reason": "关键词匹配", "needs_vision": True}
        elif any(k in text_lower for k in ["scene", "场景", "有什么", "描述"]):
            return {"intent": "scene", "confidence": 0.7, "reason": "关键词匹配", "needs_vision": True}
        elif any(k in text_lower for k in ["solver", "解题", "求解", "计算"]):
            return {"intent": "solver", "confidence": 0.7, "reason": "关键词匹配", "needs_vision": True}
        else:
            return {"intent": "chat", "confidence": 0.5, "reason": "无法解析，默认聊天", "needs_vision": False}

    def _rule_based_intent(self, user_text: str, has_images: bool) -> Dict[str, Any]:
        """基于规则的意图判断（无模型时回退）- 更严格"""
        text = user_text.lower()
        
        # 检查是否有明确的Bot唤醒词
        has_wake_word = any(name.lower() in text for name in ["韶梦", "shaomeng"])
        
        # 严格模式：没有唤醒词默认就是chat
        if not has_wake_word and not has_images:
            return {"intent": "chat", "confidence": 1.0, "reason": "无唤醒词，默认不触发", "needs_vision": False}
        
        if not has_images:
            # 有唤醒词但没有图片，检查是否是复杂问题
            solver_keywords = ["求解", "证明", "计算", "这道题", "问题"]
            if has_wake_word and any(kw in text for kw in solver_keywords):
                return {"intent": "solver", "confidence": 0.8, "reason": "规则匹配：唤醒词+solver关键词", "needs_vision": False}
            return {"intent": "chat", "confidence": 0.9, "reason": "无图片，默认聊天", "needs_vision": False}
        
        # 有图片且有唤醒词，进一步判断
        if has_wake_word:
            ocr_keywords = ["文字", "写了什么", "提取", "转文字", "识别", "ocr"]
            if any(kw in text for kw in ocr_keywords):
                return {"intent": "ocr", "confidence": 0.8, "reason": "规则匹配：唤醒词+OCR关键词", "needs_vision": True}
            
            scene_keywords = ["有什么", "是什么", "哪里", "描述", "怎么样", "看图"]
            if any(kw in text for kw in scene_keywords):
                return {"intent": "scene", "confidence": 0.8, "reason": "规则匹配：唤醒词+场景关键词", "needs_vision": True}
            
            solver_keywords = ["求解", "怎么做", "答案", "计算", "证明", "解", "做法", "题目"]
            if any(kw in text for kw in solver_keywords):
                return {"intent": "solver", "confidence": 0.8, "reason": "规则匹配：唤醒词+solver关键词", "needs_vision": True}
        
        # 默认不触发
        return {"intent": "chat", "confidence": 0.9, "reason": "规则匹配：无明确意图", "needs_vision": False}

    async def check_garbled_text(self, text: str) -> Dict[str, Any]:
        """
        检查文本是否为乱码或低质量OCR结果
        返回: {"is_garbled": bool, "reason": str, "confidence": float}
        """
        if not self.is_configured():
            return {
                "is_garbled": self._rule_check_garbled(text),
                "reason": "规则检查",
                "confidence": 0.8
            }
        
        prompt = f"""检查以下OCR识别结果是否为乱码或质量低下：

识别结果: "{text[:500]}"

判断标准：
1. 是否包含大量乱码字符（如"��"、"�"、"锟斤拷"）
2. 是否完全无意义（如纯符号、随机字符）
3. 是否只有零星几个字符，无法构成句子
4. 是否包含大量连续重复字符（如"啊啊啊啊啊"）

请以JSON格式输出：
{{
  "is_garbled": true/false,
  "reason": "说明原因",
  "confidence": 0.0-1.0
}}"""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.judge_provider,
                prompt=prompt
            )
            
            result_text = resp.completion_text.strip()
            
            if "```" in result_text:
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
                result_text = result_text.strip()
            
            data = json.loads(result_text)
            
            logger.info(f"[IntentJudge] 乱码检查结果: {data.get('is_garbled', False)} "
                       f"(原因: {data.get('reason', 'N/A')})")
            
            return {
                "is_garbled": bool(data.get("is_garbled", False)),
                "reason": data.get("reason", ""),
                "confidence": float(data.get("confidence", 0.5))
            }
            
        except Exception as e:
            logger.error(f"[IntentJudge] 乱码检查失败: {e}")
            return {
                "is_garbled": self._rule_check_garbled(text),
                "reason": f"模型检查失败，回退到规则: {str(e)}",
                "confidence": 0.5
            }

    def _rule_check_garbled(self, text: str) -> bool:
        """规则检查乱码"""
        if not text or len(text.strip()) < 3:
            return True
        
        if '�' in text or '��' in text:
            return True
        
        if re.search(r'(.)\1{5,}', text):
            return True
        
        if not re.search(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', text):
            return True
        
        return False

    async def is_complex_question(
        self,
        question: str,
        sender_info: dict,
        conv_id: str
    ) -> bool:
        """
        判断是否为复杂问题（需要调用解题模型）- 更严格的判定
        新增：用户明确要求调用解题模型时直接判定为复杂问题
        """
        # 【兜底规则】如果用户明确要求调用解题模型，直接判定为复杂问题，不走LLM
        force_solver_keywords = [
            "解题模型", "solver", "复杂问题", "深度思考", "详细解答"
        ]
        
        question_lower = question.lower()
        for keyword in force_solver_keywords:
            if keyword in question_lower:
                logger.info(f"[IntentJudge] 检测到强制调用关键词 '{keyword}'，直接判定为复杂问题")
                return True
        
        # 如果没配置判断模型，回退到长度和关键词判断
        if not self.is_configured():
            basic_complex_keywords = ["求解", "证明", "计算", "为什么"]
            # 更严格的规则：必须有明确数学/逻辑特征
            has_math_signs = bool(re.search(r'[\d\+\-\*\/\=\>\<\(\)\[\]\{\}]', question))
            is_long = len(question) > 30  # 提高长度门槛
            has_keywords = any(kw in question for kw in basic_complex_keywords)
            return (is_long and has_keywords) or (has_keywords and has_math_signs)

        # 【优化提示词】严格区分追问和复杂问题
        prompt = f"""判断以下问题是否需要专业解题模型（复杂问题）。

【严格区分标准】
复杂问题特征（必须满足）：
- 需要多步推理的数学计算、方程求解、几何证明
- 物理/化学/生物等专业学科问题，需要公式推导
- 逻辑推理题、算法设计、代码编写
- 用户明确要求详细步骤或深度解答

简单问题/追问特征（以下任一即判定为SIMPLE）：
- 日常闲聊、问候（"你好"、"在吗"）
- 简短追问（"为什么"、"详细点"、"然后呢"、"解释一下"）
- 对之前回答的评价（"不对"、"错了"、"还有呢"）
- 简单事实查询（"今天几号"、"1+1等于几"）
- 非学术性的观点讨论（"你觉得怎么样"）

【强制规则】
如果用户消息中包含以下任何意图，请直接输出 COMPLEX：
- "调用/使用/启动解题模型" 
- "开启解题模式"
- "当做复杂问题解答"

请只输出"COMPLEX"或"SIMPLE"，不要输出任何解释：

问题: "{question}"
判断结果："""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.judge_provider,
                prompt=prompt
            )
            
            result = resp.completion_text.strip().upper()
            is_complex = "COMPLEX" in result
            
            logger.info(f"[IntentJudge] 问题复杂度判断: {'COMPLEX' if is_complex else 'SIMPLE'}")
            
            await self.debugger.report_complexity_judge(
                provider_id=self.judge_provider,
                model="default",
                question=question,
                images=[],
                result=is_complex,
                sender_info=sender_info,
                conv_id=conv_id
            )
            
            return is_complex
            
        except Exception as e:
            logger.error(f"[IntentJudge] 复杂度判断失败: {e}")
            # 异常时也检查兜底关键词
            return any(kw in question_lower for kw in force_solver_keywords)

    async def is_followup_question(
        self,
        question: str,
        sender_info: dict,
        conv_id: str
    ) -> bool:
        """
        严格判断是否为追问（对之前问题的简短追问）
        返回True表示是追问，应该由主LLM处理
        """
        if not self.is_configured():
            # 规则判断
            followup_patterns = [
                r'^(为什么|怎么|然后呢|还有呢|详细|解释|展开|继续|接着|后来呢|那.+呢)[\?？]?$',
                r'^[不对|错了|好像|但是|可是].*$',
                r'^[那|所以|那么|如果].*[呢|吗|？|?]$'
            ]
            for pattern in followup_patterns:
                if re.match(pattern, question.strip()):
                    return True
            return False
        
        prompt = f"""判断以下消息是否是对之前问题的追问。

用户新消息: "{question}"

追问特征（满足任一即判定为FOLLOWUP）：
1. 简短追问词："为什么"、"详细点"、"然后呢"、"解释一下"、"还有呢"
2. 对之前回答的评价/纠正："不对"、"错了"、"好像有问题"
3. 承接性追问："那如果..."、"所以..."、"那么..."
4. 要求补充："展开说说"、"具体点"、"举个例子"

新问题特征（判定为NEW）：
- 提出了全新的、独立的问题
- 询问完全不同的话题
- 虽然是简短句子，但是独立问题（"1+1等于几"）

请以JSON格式输出：
{{
  "is_followup": true/false,
  "reason": "说明判定原因",
  "confidence": 0.0-1.0
}}"""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.judge_provider,
                prompt=prompt
            )
            
            result_text = resp.completion_text.strip()
            
            if "```" in result_text:
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
                result_text = result_text.strip()
            
            data = json.loads(result_text)
            is_followup = bool(data.get("is_followup", False))
            
            logger.info(f"[IntentJudge] 追问判断: {'是追问' if is_followup else '新问题'} "
                       f"(原因: {data.get('reason', 'N/A')})")
            
            return is_followup
            
        except Exception as e:
            logger.error(f"[IntentJudge] 追问判断失败: {e}")
            return False

    async def check_question_clarity(
        self,
        question: str,
        sender_info: dict,
        conv_id: str
    ) -> Dict[str, Any]:
        """
        检查问题是否明确，是否应该拒绝回答
        返回: {"is_clear": bool, "reason": str}
        """
        if not self.is_configured():
            # 简单规则检查
            if len(question.strip()) < 5:
                return {"is_clear": False, "reason": "问题描述过短，请详细描述您的问题"}
            if re.match(r'^[\?\！\。\，\.\,\!]+$', question.strip()):
                return {"is_clear": False, "reason": "问题只包含标点符号，请详细描述您的问题"}
            return {"is_clear": True, "reason": ""}
        
        prompt = f"""判断以下问题描述是否足够明确，是否应该拒绝回答。

用户问题: "{question}"

明确问题标准：
- 清楚描述了需要解答的内容
- 包含了必要的背景信息
- 不是纯标点或乱码
- 不是明显的不完整句子（"这道题"、"这个"、"求解"）

不明确问题示例（应拒绝）：
- "这道题怎么做"（没有题目内容）
- "求解"（过于模糊）
- "？？？"（纯标点）
- "这个答案"（缺少上下文）

请以JSON格式输出：
{{
  "is_clear": true/false,
  "reason": "如果不明确，说明原因和建议",
  "confidence": 0.0-1.0
}}"""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.judge_provider,
                prompt=prompt
            )
            
            result_text = resp.completion_text.strip()
            
            if "```" in result_text:
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
                result_text = result_text.strip()
            
            data = json.loads(result_text)
            is_clear = bool(data.get("is_clear", True))
            
            logger.info(f"[IntentJudge] 问题明确性检查: {'明确' if is_clear else '不明确'}")
            
            return {
                "is_clear": is_clear,
                "reason": data.get("reason", "问题描述不够明确，请详细描述您的问题")
            }
            
        except Exception as e:
            logger.error(f"[IntentJudge] 明确性检查失败: {e}")
            # 失败时默认允许
            return {"is_clear": True, "reason": ""}