import re
import json
import time
import os
from typing import List, Dict, Any, Optional
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain, At, BaseMessageComponent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

class LLMAtToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.valid_at_pattern = re.compile(r'\[at:(\d+)\]')
        
        # 修正： AstrBot 标准插件数据目录是 plugin_data（单数）
        self.data_dir = os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_attool")
        self.db_path = os.path.join(self.data_dir, "nickname_mapping.json")
        
        logger.info(f"[AtTool] 数据库路径: {self.db_path}")
        self._ensure_db_exists()
        
        # 缓存回复目标（仅用于辅助，不再自动插入艾特）
        self.reply_targets: Dict[int, Dict] = {}
        
    def _ensure_db_exists(self):
        """确保数据库文件和目录存在"""
        try:
            if not os.path.exists(self.data_dir):
                os.makedirs(self.data_dir, exist_ok=True)
                logger.info(f"[AtTool] 创建数据目录: {self.data_dir}")
            
            if not os.path.exists(self.db_path):
                initial_data = {}
                with open(self.db_path, 'w', encoding='utf-8') as f:
                    json.dump(initial_data, f, ensure_ascii=False, indent=2)
                logger.info(f"[AtTool] 创建空数据库文件: {self.db_path}")
            else:
                # 检查现有内容
                try:
                    with open(self.db_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    total_groups = len(data)
                    total_members = sum(len(g.get("members", {})) for g in data.values())
                    logger.info(f"[AtTool] 加载已有数据库: {total_groups} 个群, {total_members} 个成员")
                except:
                    logger.warning(f"[AtTool] 数据库文件存在但为空或损坏")
        except Exception as e:
            logger.error(f"[AtTool] 初始化数据库失败: {e}")

    def _load_db(self) -> Dict[str, Any]:
        """加载数据库"""
        try:
            if not os.path.exists(self.db_path):
                return {}
            with open(self.db_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except Exception as e:
            logger.error(f"[AtTool] 加载数据库失败: {e}")
            return {}
    
    def _save_db(self, data: Dict[str, Any], reason: str = ""):
        """保存数据库到文件，并记录日志"""
        try:
            # 确保目录存在
            os.makedirs(self.data_dir, exist_ok=True)
            
            # 原子写入：先写临时文件，再重命名，防止数据损坏
            temp_path = self.db_path + ".tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 如果文件已存在，先备份（可选）
            if os.path.exists(self.db_path):
                backup_path = self.db_path + ".backup"
                try:
                    os.replace(self.db_path, backup_path)
                except:
                    pass
            
            # 重命名临时文件为正式文件
            os.replace(temp_path, self.db_path)
            
            # 计算统计信息
            total_groups = len(data)
            total_members = sum(len(g.get("members", {})) for g in data.values())
            
            reason_str = f" [{reason}]" if reason else ""
            logger.info(f"[AtTool] 数据库保存成功{reason_str} - 群数: {total_groups}, 成员数: {total_members}, 路径: {self.db_path}")
            
            # 验证写入成功
            if os.path.exists(self.db_path):
                file_size = os.path.getsize(self.db_path)
                logger.debug(f"[AtTool] 文件大小: {file_size} bytes")
            
        except Exception as e:
            logger.error(f"[AtTool] 保存数据库失败: {e}", exc_info=True)

    @filter.on_llm_request()
    async def inject_at_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        注入 System Prompt
        """
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        
        event_id = id(event)
        self.reply_targets[event_id] = {
            "sender_id": sender_id,
            "sender_name": sender_name,
            "target_id": sender_id,
            "is_group": bool(group_id)
        }
        
        instruction = f'''
<system_protocols>

<reply_target_protocol>
    <description>判断消息应该回复给谁（非常重要，必须要有一个艾特目标）</description>
    <rules>
        <rule>默认情况：回复当前说话者 ({sender_name}[at:{sender_id}])</rule>
        <rule>例外情况：如果用户要求你提醒/通知/艾特@第三方（如"告诉李四开会"、"提醒王五交作业"），则回复目标变为那个第三方，而不是说话者</rule>
        <rule>判断依据：分析用户意图，如果消息是"让某人做某事"或"向某人传达信息"，则该"某人"是回复目标</rule>
    </rules>
    <examples>
        <example>
            <input>张三说："天气真好"</input>
            <target>张三</target>
            <action>应回复并at张三</action>
        </example>
        <example>
            <input>张三说："提醒李四明天开会"</input>
            <target>李四</target>
            <action>艾特李四并提醒开会，不再艾特张三（说话者）</action>
        </example>
    </examples>
</reply_target_protocol>

<nickname_mapping_protocol>
    <description>本地昵称数据库使用协议。数据库保存了群友的QQ号和他们被称呼的各种昵称（一个QQ号可有多个昵称）</description>
    <workflow>
        <step index="1">分析消息中提到的名字（如"提醒小明"中的"小明"）</step>
        <step index="2">调用工具 query_nickname 搜索该昵称，获取候选QQ号列表</step>
        <step index="3">如果返回多个结果，根据上下文判断最可能的一个（如最近活跃的、角色匹配的、或带有人工标记*的）</step>
        <step index="4">如果未找到映射，调用工具 get_or_update_members 同步群成员（会自动添加新映射并立即保存到文件）</step>
        <step index="5">如果群友有新称呼，调用工具 add_nickname 添加到已有QQ号下（追加，不覆盖，立即保存）</step>
    </workflow>
    <constraints>
        <constraint>一个QQ号可对应多个昵称，但一个昵称原则上对应一个QQ号（如遇重名需人工确认）</constraint>
        <constraint>不要删除已有昵称，除非用户明确要求且你确认无误</constraint>
        <constraint>使用JSON数据库，格式为：group_id -> qq_number -> nicknames: []</constraint>
    </constraints>
</nickname_mapping_protocol>

<at_mention_protocol>
    <description>艾特协议，定义如何实际艾特目标</description>
    <workflow>
        <step index="1">根据 reply_target_protocol 确定最终艾特与回复目标</step>
        <step index="2">如果目标不是当前说话者，通过 nickname_mapping_protocol 获取目标QQ号</step>
        <step index="3">在回复开头插入 [at:target_qq] （如果是私聊则不艾特）</step>
        <step index="4">构建回复内容，一定要包含一个at，指向其他目标后不另外at说话者，但是如果没有其他目标一定要at说话者</step>
    </workflow>
    <format>
        <tag>[at:qq_number]</tag>
        <example>[at:123456] 好的，已经提醒他了</example>
    </format>
</at_mention_protocol>

</system_protocols>
'''
        req.system_prompt += instruction

    @filter.llm_tool(name="query_nickname")
    async def query_nickname(self, event: AstrMessageEvent, nickname: str) -> str:
        """
        在本地数据库中查询昵称对应的QQ号。
        
        Args:
            nickname (str): 要查询的昵称（如"小明"、"张总"）
        """
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps({"status": "error", "message": "不在群聊环境中"}, ensure_ascii=False)
        
        db = self._load_db()
        group_data = db.get(str(group_id), {})
        members = group_data.get("members", {})
        
        candidates: List[Dict[str, Any]] = []
        nickname_lower = nickname.lower()
        
        for qq, info in members.items():
            nicknames = info.get("nicknames", [])
            for nn in nicknames:
                if nickname_lower in nn.lower() or nn.lower() in nickname_lower:
                    candidates.append({
                        "qq": qq,
                        "nicknames": nicknames,
                        "match_type": "exact" if nickname_lower == nn.lower() else "fuzzy"
                    })
                    break
        
        candidates.sort(key=lambda x: x["match_type"] == "exact", reverse=True)
        
        return json.dumps({
            "status": "success" if candidates else "not_found",
            "query": nickname,
            "count": len(candidates),
            "candidates": candidates,
            "suggestion": "如有多个候选，请根据上下文选择最合适的；如未找到，请调用 get_or_update_members 同步群列表"
        }, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="add_nickname")
    async def add_nickname(self, event: AstrMessageEvent, qq: str, new_nickname: str) -> str:
        """
        为已存在的QQ号添加新昵称。保存后立即写入文件。
        
        Args:
            qq (str): QQ号
            new_nickname (str): 新昵称（如"张总"、"小李"）
        """
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps({"status": "error", "message": "不在群聊环境中"}, ensure_ascii=False)
        
        db = self._load_db()
        group_key = str(group_id)
        
        if group_key not in db:
            db[group_key] = {"members": {}}
        
        members = db[group_key]["members"]
        
        if qq not in members:
            return json.dumps({
                "status": "error", 
                "message": f"QQ {qq} 不在数据库中，请先调用 get_or_update_members 同步"
            }, ensure_ascii=False)
        
        existing = members[qq].get("nicknames", [])
        if new_nickname in existing:
            return json.dumps({
                "status": "info",
                "message": f"昵称 '{new_nickname}' 已存在",
                "qq": qq,
                "all_nicknames": existing
            }, ensure_ascii=False)
        
        # 追加新昵称
        existing.append(new_nickname)
        members[qq]["nicknames"] = existing
        members[qq]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        # 立即保存
        self._save_db(db, reason=f"添加昵称 {new_nickname} 到 QQ:{qq}")
        
        return json.dumps({
            "status": "success",
            "message": f"已为 {qq} 添加昵称 '{new_nickname}'",
            "qq": qq,
            "all_nicknames": existing
        }, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="get_or_update_members")
    async def get_or_update_members(self, event: AstrMessageEvent, force_update: bool = False) -> str:
        """
        获取群成员列表并更新本地数据库。每添加一个新成员立即保存。
        
        Args:
            force_update (bool): 是否强制更新（默认False）
        """
        group_id = event.get_group_id()
        if not group_id:
            return json.dumps({"status": "error", "message": "不在群聊环境中"}, ensure_ascii=False)
        
        if not isinstance(event, AiocqhttpMessageEvent):
            return json.dumps({"status": "error", "message": "平台不支持"}, ensure_ascii=False)
        
        try:
            logger.info(f"[AtTool] 开始获取群 {group_id} 成员列表...")
            raw_members = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
            
            if not raw_members:
                logger.warning(f"[AtTool] 获取群 {group_id} 成员列表为空")
                return json.dumps({"status": "error", "message": "获取失败或权限不足"}, ensure_ascii=False)
            
            logger.info(f"[AtTool] 获取到 {len(raw_members)} 个群成员")
            
            db = self._load_db()
            group_key = str(group_id)
            
            if group_key not in db:
                db[group_key] = {"members": {}}
                logger.info(f"[AtTool] 创建新群记录: {group_key}")
            
            existing_members = db[group_key]["members"]
            added_count = 0
            skipped_count = 0
            
            for m in raw_members:
                user_id = str(m.get("user_id", ""))
                nickname = m.get("nickname", "")
                card = m.get("card", "")
                
                # 判断是否需要跳过（仅当存在且不强制更新时跳过）
                if user_id in existing_members and not force_update:
                    skipped_count += 1
                    continue
                
                # 新增或强制更新时，构造/更新成员信息
                nicknames: List[str] = []
                display_name = nickname or card or user_id
                
                if nickname:
                    nicknames.append(nickname)
                if card and card != nickname:
                    nicknames.append(card)
                
                # 如果是强制更新且成员已存在，保留原有 nicknames？这里选择覆盖（简化处理）
                # 但为保留已有昵称，可合并新旧昵称（去重）
                if force_update and user_id in existing_members:
                    old_nicknames = existing_members[user_id].get("nicknames", [])
                    # 合并新获取的 nickname/card 与旧的，去重
                    combined = set(old_nicknames + nicknames)
                    nicknames = list(combined)
                
                existing_members[user_id] = {
                    "qq": user_id,
                    "nicknames": nicknames,
                    "role": m.get("role", "member"),
                    "first_seen": existing_members.get(user_id, {}).get("first_seen", time.strftime("%Y-%m-%d %H:%M:%S")),
                    "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                added_count += 1
                # 不再每次保存，改为最后统一保存
            
            # 统一保存一次
            self._save_db(db, reason=f"群{group_id}同步完成，新增{added_count}人，跳过{skipped_count}人")
            
            return json.dumps({
                "status": "success",
                "message": f"同步完成：新增 {added_count} 人，跳过 {skipped_count} 人（已有数据）",
                "total_in_db": len(existing_members),
                "data_saved": True,
                "path": self.db_path
            }, ensure_ascii=False, indent=2)
            
        except Exception as e:
            logger.error(f"[AtTool] 同步群成员失败: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

    # 新增手动同步命令
    @filter.command("sync_members")
    async def sync_members_command(self, event: AstrMessageEvent):
        """手动同步当前群成员列表（调试用）"""
        result = await self.get_or_update_members(event, force_update=True)
        yield event.plain_result(str(result))

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        处理消息中的[at:qq]标签，转换为真实艾特组件。
        注意：不再自动添加任何艾特，完全由 LLM 输出的 [at:qq] 决定。
        """
        if not event.get_group_id():
            return
            
        result = event.get_result()
        if not result or not result.chain:
            return
        
        # 不再使用 reply_targets 自动添加艾特，仅处理已有的 [at:qq] 标签
        # 但仍然保留清理缓存（可选）
        event_id = id(event)
        
        new_chain: List[BaseMessageComponent] = []
        found_explicit_at = False
        
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0
                
                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()
                    found_explicit_at = True
                    
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))
                    
                    target_id = match.group(1)
                    new_chain.append(At(qq=target_id))
                    new_chain.append(Plain(" "))
                    last_idx = end
                
                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                new_chain.append(comp)
        
        # 如果没有找到任何 [at:qq] 标签，且原有结果有纯文本，保持原样（不自动添加）
        # 注意：found_explicit_at 仅用于标记，这里不再自动插入
        
        result.chain = new_chain
        
        # 清理缓存
        if event_id in self.reply_targets:
            del self.reply_targets[event_id]