"""LLM Debugger

版本: 1.3.5 (Fixed)
作者: 韶虹CYun
"""

import os
import json
import asyncio
import traceback
import time
from datetime import datetime
from typing import Dict, Any, Set, Optional, Tuple

from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

try:
    from astrbot.api.provider import ProviderRequest, LLMResponse
except ImportError:
    ProviderRequest = None
    LLMResponse = None


@register("llm_debugger", "韶虹CYun", "LLM 调用监控调试器（带WebUI）", "1.3.5")
class LLMDebugger(Star):
    """LLM 调用监控调试器（带WebUI）- 支持MoreChatPlus数据查看 + 抓包功能"""

    DEFAULT_PORT = 6188
    DEFAULT_PASSWORD = "llm_debugger1357"
    DEFAULT_MAX_RECORDS = 500
    DEFAULT_MAX_CAPTURE_RECORDS = 1000  # 抓包数据单独限制

    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.db = None
        # 新增：抓包数据库
        self.capture_db = None
        self.capture_clients: Set = set()
        self.web_server = None
        self.connected_clients: Set = set()
        self._shutdown_event = None
        self._server_stopped = asyncio.Event()
        self._processed_ids: Dict[Tuple[Optional[str], str], float] = {}
        self._cleanup_task = None

        # 关键：尽早注册到 context
        self._register_to_context()

    def _register_to_context(self):
        """将debugger实例注册到context，方便其他插件获取"""
        try:
            if not hasattr(self.context, '_plugin_instances'):
                self.context._plugin_instances = {}
            self.context._plugin_instances['llm_debugger'] = self
            logger.info("[LLMDebugger] 已注册到 context._plugin_instances")
        except Exception as e:
            logger.debug("[LLMDebugger] 注册到 _plugin_instances 失败: " + str(e))

        try:
            if hasattr(self.context, 'star_registry'):
                if isinstance(self.context.star_registry, dict):
                    self.context.star_registry['llm_debugger'] = self
                    logger.info("[LLMDebugger] 已注册到 context.star_registry")
        except Exception as e:
            logger.debug("[LLMDebugger] 注册到 star_registry 失败: " + str(e))

    async def initialize(self):
        """初始化插件"""
        try:
            import aiosqlite
            from quart import Quart, render_template, websocket, request, session, redirect, abort, render_template_string, jsonify
            from quart_cors import cors
            import functools
            import base64
        except ImportError as e:
            logger.error("[LLMDebugger] 缺少依赖库: " + str(e) + "，请安装: pip install aiosqlite quart quart-cors")
            return

        plugin_config = self.context.get_config("llm_debugger") or {}
        port = plugin_config.get("port", self.DEFAULT_PORT)
        password = plugin_config.get("password", self.DEFAULT_PASSWORD)
        max_records = plugin_config.get("max_records", self.DEFAULT_MAX_RECORDS)
        max_capture = plugin_config.get("max_capture_records", self.DEFAULT_MAX_CAPTURE_RECORDS)

        pwd_status = "已设置" if password else "未设置"
        logger.info("[LLMDebugger] 配置加载: port=" + str(port) + ", password=" + pwd_status + ", max_records=" + str(max_records) + ", max_capture=" + str(max_capture))

        data_dir = "/AstrBot/data"
        if not os.path.exists(data_dir):
            data_dir = os.path.join(os.path.dirname(__file__), "data")
        db_dir = os.path.join(data_dir, "plugin_data", "llm_debugger")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "logs.db")

        self.db = Database(db_path, max_records)
        await self.db.init()

        # 新增：初始化抓包数据库（独立文件）
        capture_db_path = os.path.join(db_dir, "capture.db")
        self.capture_db = CaptureDatabase(capture_db_path, max_capture)
        await self.capture_db.init()
        logger.info("[LLMDebugger] 抓包数据库已初始化")

        self._cleanup_task = asyncio.create_task(self._cleanup_cache())
        self._server_stopped.clear()
        self._shutdown_event = asyncio.Event()
        self.web_server = asyncio.create_task(self._start_web_server(port, password))
        logger.info("[LLMDebugger] WebUI 已启动: http://0.0.0.0:" + str(port))

    async def _cleanup_cache(self):
        """定期清理去重缓存"""
        while True:
            await asyncio.sleep(3600)
            try:
                now = time.time()
                to_remove = [key for key, ts in self._processed_ids.items() if now - ts > 3600]
                for key in to_remove:
                    del self._processed_ids[key]
                logger.debug("[LLMDebugger] 清理了 " + str(len(to_remove)) + " 条去重缓存记录")
            except Exception as e:
                logger.error("[LLMDebugger] 清理缓存失败: " + str(e))

    # ========== 新增：抓包功能核心方法 ==========

    async def capture_llm_request(self, event: AstrMessageEvent, req):
        """抓包：捕获经过所有插件处理后的最终LLM请求

        与record_llm_call不同，这个专门用于技术调试，捕获完整的原始结构
        """
        if not self.capture_db:
            return

        # 延迟捕获，确保获取最终状态（其他插件已修改完毕）
        asyncio.create_task(self._delayed_capture_request(event, req))

    async def _delayed_capture_request(self, event: AstrMessageEvent, req, delay: float = 0.5):
        """延迟抓包，捕获最终请求状态"""
        try:
            await asyncio.sleep(delay)

            # 获取会话ID
            conv_id = self._extract_conv_id(req) or str(int(time.time() * 1000))

            # 提取完整请求结构
            capture_data = {
                "timestamp": datetime.now().isoformat(),
                "phase": "request",
                "conversation_id": conv_id,
                "sender": {
                    "id": event.get_sender_id() if event else "unknown",
                    "name": event.get_sender_name() if event else "unknown",
                    "group_id": getattr(event, 'get_group_id', lambda: None)() if event and hasattr(event, 'get_group_id') else None,
                    "platform": event.get_platform_name() if event else "unknown"
                },
                "raw_request": self._extract_full_request(req),
                "extracted_prompt": self._extract_prompt_text(req),
                "model": self._extract_model(req),
                "system_prompt": self._extract_system_prompt(req),
                "contexts": self._extract_contexts(req),
                "metadata": {
                    "has_images": self._has_images(req),
                    "req_type": type(req).__name__ if req else None,
                    "plugin_version": "1.3.5"
                }
            }

            # 保存到抓包数据库
            record_id = await self.capture_db.save_record(capture_data)
            capture_data["id"] = record_id

            # 广播到抓包WebSocket客户端
            await self._broadcast_capture(capture_data)

            logger.debug("[LLMDebugger] 已抓包请求: conv_id=" + str(conv_id) + ", model=" + str(capture_data.get('model', 'unknown')))

        except Exception as e:
            err_msg = "[LLMDebugger] 抓包请求失败: " + str(e) + "\n" + traceback.format_exc()
            logger.error(err_msg)

    async def capture_llm_response(self, event: AstrMessageEvent, resp):
        """抓包：捕获LLM响应"""
        if not self.capture_db:
            return

        try:
            conv_id = self._extract_conv_id_from_resp(resp) or str(int(time.time() * 1000))

            capture_data = {
                "timestamp": datetime.now().isoformat(),
                "phase": "response",
                "conversation_id": conv_id,
                "sender": {
                    "id": event.get_sender_id() if event else "unknown",
                    "name": event.get_sender_name() if event else "unknown"
                },
                "response_text": self._extract_response_text(resp),
                "model": self._extract_model_from_resp(resp),
                "usage": self._extract_usage(resp),
                "raw_response": self._make_serializable(resp) if resp else None,
                "metadata": {
                    "resp_type": type(resp).__name__ if resp else None,
                    "timestamp_ms": int(time.time() * 1000)
                }
            }

            record_id = await self.capture_db.save_record(capture_data)
            capture_data["id"] = record_id
            await self._broadcast_capture(capture_data)

            logger.debug("[LLMDebugger] 已抓包响应: conv_id=" + str(conv_id))

        except Exception as e:
            logger.error("[LLMDebugger] 抓包响应失败: " + str(e))

    def _extract_full_request(self, req) -> dict:
        """提取完整的请求对象结构"""
        if not req:
            return {}

        result = {}
        # 遍历所有属性，捕获完整状态
        for attr in ['prompt', 'text', 'content', 'messages', 'system_prompt', 
                     'system_instruction', 'contexts', 'model', 'temperature',
                     'top_p', 'max_tokens', 'image_urls', 'images', 'extra_params']:
            if hasattr(req, attr):
                try:
                    val = getattr(req, attr)
                    if val is not None:
                        result[attr] = self._make_serializable(val)
                except:
                    pass

        return result

    def _extract_prompt_text(self, req) -> str:
        """提取最终发送的Prompt文本"""
        if not req:
            return ""

        # 尝试多种可能的属性
        if hasattr(req, 'prompt') and req.prompt:
            return str(req.prompt)
        if hasattr(req, 'text') and req.text:
            return str(req.text)
        if hasattr(req, 'content') and req.content:
            return str(req.content)
        if hasattr(req, 'messages') and req.messages:
            # 提取最后一条user消息
            msgs = req.messages
            if msgs and len(msgs) > 0:
                last = msgs[-1]
                if isinstance(last, dict):
                    return last.get('content', '') or last.get('text', '')
                elif hasattr(last, 'content'):
                    return str(last.content)
        return ""

    def _extract_system_prompt(self, req) -> str:
        """提取System Prompt"""
        if not req:
            return ""
        if hasattr(req, 'system_prompt') and req.system_prompt:
            return str(req.system_prompt)
        if hasattr(req, 'system_instruction') and req.system_instruction:
            return str(req.system_instruction)
        # 从messages中提取system角色
        if hasattr(req, 'messages') and req.messages:
            for msg in req.messages:
                if isinstance(msg, dict) and msg.get('role') == 'system':
                    return msg.get('content', '')
                elif hasattr(msg, 'role') and msg.role == 'system':
                    return getattr(msg, 'content', '') or getattr(msg, 'text', '')
        return ""

    def _extract_contexts(self, req) -> list:
        """提取上下文历史"""
        if not req:
            return []
        if hasattr(req, 'contexts') and req.contexts:
            return self._make_serializable(req.contexts)
        if hasattr(req, 'messages') and req.messages:
            # 排除最后一条（当前请求）和system消息
            msgs = req.messages
            if len(msgs) > 1:
                contexts = []
                for msg in msgs[:-1]:
                    if isinstance(msg, dict):
                        if msg.get('role') != 'system':
                            contexts.append(msg)
                    else:
                        contexts.append(self._make_serializable(msg))
                return contexts
        return []

    def _extract_model(self, req) -> str:
        """提取模型名称"""
        if not req:
            return "unknown"
        if hasattr(req, 'model') and req.model:
            return str(req.model)
        if hasattr(req, 'get_model') and callable(getattr(req, 'get_model')):
            try:
                return str(req.get_model())
            except:
                pass
        return "unknown"

    def _has_images(self, req) -> bool:
        """检查是否包含图片"""
        if not req:
            return False
        if hasattr(req, 'image_urls') and req.image_urls:
            return len(req.image_urls) > 0
        if hasattr(req, 'images') and req.images:
            return len(req.images) > 0
        return False

    def _extract_conv_id(self, req) -> Optional[str]:
        """从请求提取会话ID"""
        if not req:
            return None
        for attr in ['conversation_id', 'session_id', 'id', 'chat_id']:
            if hasattr(req, attr):
                val = getattr(req, attr)
                if val:
                    return str(val)
        return None

    def _extract_conv_id_from_resp(self, resp) -> Optional[str]:
        """从响应提取会话ID"""
        if not resp:
            return None
        for attr in ['conversation_id', 'session_id', 'id']:
            if hasattr(resp, attr):
                val = getattr(resp, attr)
                if val:
                    return str(val)
        return None

    def _extract_response_text(self, resp) -> str:
        """提取响应文本"""
        if not resp:
            return ""
        if isinstance(resp, str):
            return resp
        for attr in ['completion_text', 'content', 'message', 'text', 'response']:
            if hasattr(resp, attr):
                val = getattr(resp, attr)
                if val:
                    return str(val)
        return str(resp)

    def _extract_model_from_resp(self, resp) -> str:
        """从响应提取模型"""
        if not resp:
            return "unknown"
        if hasattr(resp, 'model') and resp.model:
            return str(resp.model)
        return "unknown"

    def _extract_usage(self, resp) -> Optional[dict]:
        """提取Token用量"""
        if not resp:
            return None
        if hasattr(resp, 'usage') and resp.usage:
            return self._make_serializable(resp.usage)
        if hasattr(resp, 'get_usage') and callable(getattr(resp, 'get_usage')):
            try:
                return self._make_serializable(resp.get_usage())
            except:
                pass
        return None

    async def _broadcast_capture(self, data: Dict[str, Any]):
        """广播抓包数据到专用WebSocket"""
        if not self.capture_clients:
            return
        message = json.dumps(data, ensure_ascii=False, default=str)
        disconnected = set()
        for ws in self.capture_clients:
            try:
                await ws.send(message)
            except Exception:
                disconnected.add(ws)
        self.capture_clients -= disconnected

    # ========== 公共方法：供其他插件手动记录 LLM 调用 ==========
    async def record_llm_call(self, data: dict):
        """供其他插件调用的公共方法，用于记录 LLM 请求或响应"""
        if not self.db:
            logger.debug("[LLMDebugger] 数据库未初始化，无法记录")
            return

        try:
            phase = data.get("phase")
            if phase not in ("request", "response"):
                logger.warning("[LLMDebugger] 忽略无效 phase: " + str(phase))
                return

            # 记录来源信息
            source = data.get("source", {})
            plugin_name = source.get("plugin", "unknown")
            purpose = source.get("purpose", "unknown")

            logger.debug("[LLMDebugger] 收到上报: phase=" + str(phase) + ", plugin=" + plugin_name + ", purpose=" + purpose)

            record_data = {
                "timestamp": data.get("timestamp", datetime.now().isoformat()),
                "type": phase,
                "conversation_id": data.get("conversation_id"),
                "sender": data.get("sender", {}),
                "source": source,
            }

            if phase == "request":
                # 处理请求数据
                prompt = data.get("prompt", "")
                contexts = data.get("contexts", [])
                system_prompt = data.get("system_prompt", "")

                record_data.update({
                    "message": {
                        "raw": prompt,
                        "formatted_prompt": prompt,
                        "image_urls": data.get("images", []),
                    },
                    "llm_config": {
                        "model": data.get("model", "unknown"),
                        "system_prompt": system_prompt,
                        "contexts": self._make_serializable(contexts),
                        "contexts_count": len(contexts),
                    }
                })

                logger.debug("[LLMDebugger] 记录请求: model=" + str(data.get('model')) + ", prompt_length=" + str(len(prompt)) + ", contexts_count=" + str(len(contexts)))
            else:
                # 处理响应数据
                record_data.update({
                    "response": {
                        "text": data.get("response", ""),
                        "model": data.get("model"),
                        "raw": data.get("raw_response"),
                    },
                    "usage": data.get("usage"),
                })

                logger.debug("[LLMDebugger] 记录响应: model=" + str(data.get('model')) + ", response_length=" + str(len(data.get('response', ''))))

            safe_data = self._make_serializable(record_data)
            record_id = await self.db.save_record(safe_data)
            safe_data["id"] = record_id
            await self._broadcast(safe_data)

            # 更新去重缓存
            conv_id = data.get("conversation_id")
            cache_key = (conv_id, phase)
            self._processed_ids[cache_key] = time.time()

            logger.info("[LLMDebugger] 已记录 " + phase + " 来自 " + plugin_name + "/" + purpose + ", record_id=" + str(record_id))

        except Exception as e:
            err_msg = "[LLMDebugger] record_llm_call 失败: " + str(e) + "\n" + traceback.format_exc()
            logger.error(err_msg)

    # ========== MoreChatPlus 数据库查看功能 ==========
    async def _get_morechatplus_db_path(self) -> Optional[str]:
        """获取MoreChatPlus数据库路径"""
        possible_paths = [
            "/AstrBot/data/plugin_data/morechatplus/chat_data.db",
            os.path.join(os.path.dirname(__file__), "..", "..", "data", "plugin_data", "morechatplus", "chat_data.db"),
            os.path.join(os.path.dirname(__file__), "data", "plugin_data", "morechatplus", "chat_data.db"),
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None

    async def _query_morechatplus_db(self, query_type: str, limit: int = 50, offset: int = 0, origin: str = None):
        """查询MoreChatPlus数据库"""
        import aiosqlite
        db_path = await self._get_morechatplus_db_path()
        if not db_path:
            return {"error": "MoreChatPlus数据库未找到"}

        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row

                if query_type == "messages":
                    if origin:
                        cursor = await db.execute(
                            "SELECT * FROM messages WHERE origin = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                            (origin, limit, offset)
                        )
                    else:
                        cursor = await db.execute(
                            "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                            (limit, offset)
                        )
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]

                elif query_type == "user_profiles":
                    if origin:
                        cursor = await db.execute(
                            "SELECT * FROM user_profiles WHERE origin = ?",
                            (origin,)
                        )
                    else:
                        cursor = await db.execute("SELECT * FROM user_profiles")
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]

                elif query_type == "summaries":
                    if origin:
                        cursor = await db.execute(
                            "SELECT * FROM context_summaries WHERE origin = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                            (origin, limit, offset)
                        )
                    else:
                        cursor = await db.execute(
                            "SELECT * FROM context_summaries ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                            (limit, offset)
                        )
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]

                elif query_type == "origins":
                    cursor = await db.execute("SELECT DISTINCT origin FROM messages")
                    rows = await cursor.fetchall()
                    return [row["origin"] for row in rows]

                elif query_type == "stats":
                    stats = {}
                    cursor = await db.execute("SELECT COUNT(*) as count FROM messages")
                    stats["total_messages"] = (await cursor.fetchone())["count"]

                    cursor = await db.execute("SELECT COUNT(*) as count FROM user_profiles")
                    stats["total_profiles"] = (await cursor.fetchone())["count"]

                    cursor = await db.execute("SELECT COUNT(*) as count FROM context_summaries")
                    stats["total_summaries"] = (await cursor.fetchone())["count"]

                    cursor = await db.execute("SELECT COUNT(DISTINCT origin) as count FROM messages")
                    stats["total_origins"] = (await cursor.fetchone())["count"]

                    return stats

                else:
                    return {"error": "未知查询类型: " + query_type}

        except Exception as e:
            logger.error("[LLMDebugger] 查询MoreChatPlus数据库失败: " + str(e))
            return {"error": str(e)}

    # ========== Web服务器 ==========
    async def _start_web_server(self, port: int, password: str):
        """启动Web服务器"""
        from quart import Quart, render_template, websocket, request, session, redirect, abort, render_template_string, jsonify
        from quart_cors import cors
        import functools
        import base64
        from werkzeug.exceptions import NotFound

        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        if not os.path.isdir(template_dir):
            logger.error("[LLMDebugger] 模板文件夹不存在: " + template_dir)
            return

        app = Quart(__name__, template_folder=template_dir)
        app = cors(app)
        app.secret_key = os.urandom(24)

        logger.info("[LLMDebugger] 正在初始化 Web 服务器...")

        @app.errorhandler(Exception)
        async def handle_exception(e):
            if isinstance(e, NotFound):
                path = request.path
                logger.debug("[LLMDebugger] 404 Not Found: " + path)
                return "Not Found", 404
            logger.error("[LLMDebugger] 未捕获的异常: " + str(e) + "\n" + traceback.format_exc())
            return "Internal Server Error", 500

        def check_auth():
            if not password:
                return True
            if session.get("authenticated"):
                return True
            auth = request.headers.get("Authorization")
            if auth and auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[6:]).decode("utf-8")
                    _, pwd = decoded.split(":", 1)
                    if pwd == password:
                        return True
                except:
                    pass
            return False

        def require_auth(f):
            @functools.wraps(f)
            async def decorated(*args, **kwargs):
                if not check_auth():
                    if request.path.startswith("/api/") or request.path == "/ws" or request.path == "/ws_capture":
                        abort(401)
                    else:
                        return redirect("/login")
                return await f(*args, **kwargs)
            return decorated

        LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Login</title>
<style>body{margin:0;padding:0;background:#0f172a;color:#e2e8f0;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}
.box{background:#1e293b;padding:2rem;border-radius:12px;width:100%;max-width:400px}
h2{color:#60a5fa;text-align:center}input{width:100%;padding:0.75rem;margin:0.5rem 0;border:1px solid #334155;background:#0f172a;color:#e2e8f0;border-radius:6px;box-sizing:border-box}
button{width:100%;padding:0.75rem;background:#3b82f6;color:white;border:none;border-radius:6px;cursor:pointer;font-size:1rem;margin-top:1rem}
.error{color:#ef4444;text-align:center;margin-top:0.5rem}</style></head>
<body><div class="box"><h2>🔒 LLM Debugger</h2><form method="post">
<input type="password" name="password" placeholder="请输入访问密码" required autofocus>
<button type="submit">进入</button>{% if error %}<div class="error">{{ error }}</div>{% endif %}</form></div></body></html>"""

        @app.route("/login", methods=["GET", "POST"])
        async def login():
            if not password:
                return redirect("/")
            if request.method == "POST":
                form = await request.form
                if form.get("password") == password:
                    session["authenticated"] = True
                    return redirect("/")
                return await render_template_string(LOGIN_HTML, error="密码错误")
            return await render_template_string(LOGIN_HTML, error="")
        logger.info("[LLMDebugger] 注册路由: /login")

        @app.route("/logout")
        async def logout():
            session.pop("authenticated", None)
            return redirect("/login")
        logger.info("[LLMDebugger] 注册路由: /logout")

        @app.route("/")
        @require_auth
        async def index():
            try:
                return await render_template("index.html")
            except Exception as e:
                logger.error("[LLMDebugger] 渲染首页失败: " + str(e) + "\n" + traceback.format_exc())
                return "Internal Server Error", 500
        logger.info("[LLMDebugger] 注册路由: /")

        @app.route("/api/recent", methods=["GET"])
        @require_auth
        async def get_recent():
            try:
                records = await self.get_recent_records(100)
                return jsonify(records)
            except Exception as e:
                logger.error("[LLMDebugger] 获取最近记录失败: " + str(e) + "\n" + traceback.format_exc())
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/recent")

        # ===== MoreChatPlus 数据查看 API =====
        @app.route("/api/morechatplus/stats", methods=["GET"])
        @require_auth
        async def get_morechatplus_stats():
            try:
                stats = await self._query_morechatplus_db("stats")
                return jsonify(stats)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/morechatplus/stats")

        @app.route("/api/morechatplus/origins", methods=["GET"])
        @require_auth
        async def get_morechatplus_origins():
            try:
                origins = await self._query_morechatplus_db("origins")
                return jsonify({"origins": origins})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/morechatplus/origins")

        @app.route("/api/morechatplus/messages", methods=["GET"])
        @require_auth
        async def get_morechatplus_messages():
            try:
                args = request.args
                limit = min(int(args.get("limit", 50)), 100)
                offset = int(args.get("offset", 0))
                origin = args.get("origin")
                messages = await self._query_morechatplus_db("messages", limit, offset, origin)
                return jsonify({"messages": messages, "count": len(messages)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/morechatplus/messages")

        @app.route("/api/morechatplus/profiles", methods=["GET"])
        @require_auth
        async def get_morechatplus_profiles():
            try:
                args = request.args
                origin = args.get("origin")
                profiles = await self._query_morechatplus_db("user_profiles", origin=origin)
                return jsonify({"profiles": profiles, "count": len(profiles)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/morechatplus/profiles")

        @app.route("/api/morechatplus/summaries", methods=["GET"])
        @require_auth
        async def get_morechatplus_summaries():
            try:
                args = request.args
                limit = min(int(args.get("limit", 50)), 100)
                offset = int(args.get("offset", 0))
                origin = args.get("origin")
                summaries = await self._query_morechatplus_db("summaries", limit, offset, origin)
                return jsonify({"summaries": summaries, "count": len(summaries)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/morechatplus/summaries")

        # ========== 新增：抓包API路由 ==========
        @app.route("/api/capture", methods=["GET"])
        @require_auth
        async def get_capture():
            try:
                limit = min(int(request.args.get("limit", 100)), 200)
                records = await self.capture_db.get_recent_records(limit)
                return jsonify(records)
            except Exception as e:
                logger.error("[LLMDebugger] 获取抓包记录失败: " + str(e))
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/capture (GET)")

        @app.route("/api/capture/clear", methods=["POST"])
        @require_auth
        async def clear_capture():
            try:
                await self.capture_db.clear_all()
                return jsonify({"success": True, "message": "抓包数据已清空"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        logger.info("[LLMDebugger] 注册路由: /api/capture/clear (POST)")

        # 原有的WebSocket路由
        @app.websocket("/ws")
        async def ws():
            ws_obj = websocket._get_current_object()
            self.register_ws_client(ws_obj)
            try:
                records = await self.get_recent_records(50)
                for record in reversed(records):
                    await ws_obj.send(json.dumps(record, ensure_ascii=False))
                while True:
                    await ws_obj.receive()
            except Exception as e:
                logger.debug("[LLMDebugger] WebSocket 客户端断开: " + str(e))
            finally:
                self.unregister_ws_client(ws_obj)
        logger.info("[LLMDebugger] 注册 WebSocket: /ws")

        # ========== 新增：抓包专用WebSocket ==========
        @app.websocket("/ws_capture")
        async def ws_capture():
            ws_obj = websocket._get_current_object()
            self.capture_clients.add(ws_obj)
            try:
                # 发送最近50条抓包记录
                records = await self.capture_db.get_recent_records(50)
                for record in reversed(records):
                    await ws_obj.send(json.dumps(record, ensure_ascii=False))
                while True:
                    await ws_obj.receive()
            except Exception as e:
                logger.debug("[LLMDebugger] 抓包WebSocket客户端断开: " + str(e))
            finally:
                self.capture_clients.discard(ws_obj)
        logger.info("[LLMDebugger] 注册 WebSocket: /ws_capture")

        try:
            logger.info("[LLMDebugger] 启动服务器在端口 " + str(port))
            await app.run_task(
                host='0.0.0.0',
                port=port,
                debug=False,
                shutdown_trigger=self._shutdown_event.wait
            )
        except asyncio.CancelledError:
            logger.info("[LLMDebugger] Web服务器任务被取消")
            raise
        except Exception as e:
            logger.error("[LLMDebugger] Web 服务器运行失败: " + str(e) + "\n" + traceback.format_exc())
        finally:
            self._server_stopped.set()

    # ========== 事件监听 ==========
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """监听LLM请求 - 延迟记录以捕获最终请求内容"""
        if not self.db:
            return

        # 关键改动：延迟执行记录，确保其他插件（如 MoreChatPlus）先修改完 req
        asyncio.create_task(self._delayed_record_request(event, req))

        # 新增：同时触发抓包（独立流程）
        asyncio.create_task(self.capture_llm_request(event, req))

    async def _delayed_record_request(self, event: AstrMessageEvent, req, delay: float = 0.3):
        """延迟记录请求，确保捕获被其他插件修改后的最终内容"""
        try:
            # 等待一小段时间，让 MoreChatPlus 等插件完成 on_llm_request 处理
            await asyncio.sleep(delay)

            # 现在 req 已经被其他插件（如 MoreChatPlus）修改过了
            await self._do_record_request(event, req)

        except Exception as e:
            err_msg = "[LLMDebugger] 延迟记录请求失败: " + str(e)
            logger.error(err_msg)
            logger.error(traceback.format_exc())

    async def _do_record_request(self, event: AstrMessageEvent, req):
        """实际执行请求记录"""
        try:
            # 获取会话ID，用于去重
            conv_id = None
            if hasattr(req, 'conversation_id') and req.conversation_id:
                conv_id = req.conversation_id
            elif hasattr(req, 'session_id') and req.session_id:
                conv_id = req.session_id
            elif hasattr(req, 'id') and req.id:
                conv_id = req.id
            else:
                conv_id = str(hash(str(req)) % 10000000)

            cache_key = (conv_id, "request")

            # 检查是否重复（1秒内相同ID视为重复）
            if cache_key in self._processed_ids:
                last_time = self._processed_ids[cache_key]
                if time.time() - last_time < 1.0:
                    logger.debug("[LLMDebugger] 跳过重复的请求记录: " + str(conv_id))
                    return

            # 获取Prompt（这时候已经被 MoreChatPlus 修改过了）
            prompt = ""
            if hasattr(req, 'prompt') and req.prompt:
                prompt = req.prompt
            elif hasattr(req, 'text') and req.text:
                prompt = req.text
            elif hasattr(req, 'content') and req.content:
                prompt = req.content
            elif hasattr(req, 'messages') and req.messages:
                messages = req.messages
                if messages and len(messages) > 0:
                    last_msg = messages[-1]
                    if isinstance(last_msg, dict):
                        prompt = last_msg.get('content', '') or last_msg.get('text', '')
                    elif hasattr(last_msg, 'content'):
                        prompt = last_msg.content
                    elif hasattr(last_msg, 'text'):
                        prompt = last_msg.text

            if not prompt and event:
                prompt = event.message_str or ""

            # 获取Contexts
            contexts = []
            try:
                if hasattr(req, 'contexts') and req.contexts:
                    contexts = req.contexts
                elif hasattr(req, 'get_contexts') and callable(getattr(req, 'get_contexts')):
                    contexts = req.get_contexts()
                elif hasattr(req, 'messages') and req.messages:
                    msgs = req.messages
                    if msgs and len(msgs) > 1:
                        contexts = msgs[:-1]
            except Exception as e:
                logger.debug("[LLMDebugger] 获取contexts失败: " + str(e))

            # 获取System Prompt
            system_prompt = ""
            try:
                if hasattr(req, 'system_prompt') and req.system_prompt:
                    system_prompt = req.system_prompt
                elif hasattr(req, 'get_system_prompt') and callable(getattr(req, 'get_system_prompt')):
                    system_prompt = req.get_system_prompt()
                elif hasattr(req, 'system_instruction') and req.system_instruction:
                    system_prompt = req.system_instruction
            except Exception as e:
                logger.debug("[LLMDebugger] 获取system_prompt失败: " + str(e))

            # 获取Model
            model = "default"
            try:
                if hasattr(req, 'model') and req.model:
                    model = req.model
                elif hasattr(req, 'get_model') and callable(getattr(req, 'get_model')):
                    model = req.get_model()
            except Exception as e:
                logger.debug("[LLMDebugger] 获取model失败: " + str(e))

            # 获取Image URLs
            image_urls = []
            try:
                if hasattr(req, 'image_urls') and req.image_urls:
                    image_urls = req.image_urls
                elif hasattr(req, 'get_image_urls') and callable(getattr(req, 'get_image_urls')):
                    image_urls = req.get_image_urls()
                elif hasattr(req, 'images') and req.images:
                    image_urls = req.images
            except Exception as e:
                logger.debug("[LLMDebugger] 获取image_urls失败: " + str(e))

            # 构建记录数据
            data = {
                "timestamp": datetime.now().isoformat(),
                "type": "request",
                "conversation_id": conv_id,
                "sender": {
                    "id": event.get_sender_id() if event else "unknown",
                    "name": event.get_sender_name() if event else "unknown",
                    "group_id": getattr(event, 'get_group_id', lambda: None)() if event and hasattr(event, 'get_group_id') else None,
                    "platform": event.get_platform_name() if event else "unknown"
                },
                "message": {
                    "raw": event.message_str if event else prompt,
                    "formatted_prompt": prompt,
                    "image_urls": self._make_serializable(image_urls),
                },
                "llm_config": {
                    "model": model,
                    "system_prompt": system_prompt,
                    "contexts_count": len(contexts),
                    "contexts": self._make_serializable(contexts)
                }
            }

            safe_data = self._make_serializable(data)
            record_id = await self.db.save_record(safe_data)
            safe_data["id"] = record_id
            await self._broadcast(safe_data)
            self._processed_ids[cache_key] = time.time()

            log_msg = "[LLMDebugger] 已记录最终请求: conv_id=" + str(conv_id) + ", model=" + model + ", contexts=" + str(len(contexts)) + ", prompt_length=" + str(len(prompt))
            logger.info(log_msg)

            if len(prompt) > 1000:
                logger.info("[LLMDebugger] 检测到长 Prompt（可能已注入上下文）: " + prompt[:100] + "...")

        except Exception as e:
            err_msg = "[LLMDebugger] 记录请求失败: " + str(e)
            logger.error(err_msg)
            logger.error(traceback.format_exc())

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """监听LLM响应 - 增强版"""
        if not self.db:
            return
        try:
            # 获取会话ID
            conv_id = None
            if hasattr(resp, 'conversation_id') and resp.conversation_id:
                conv_id = resp.conversation_id
            elif hasattr(resp, 'session_id') and resp.session_id:
                conv_id = resp.session_id
            elif hasattr(resp, 'id') and resp.id:
                conv_id = resp.id
            else:
                conv_id = str(hash(str(resp)) % 10000000)

            cache_key = (conv_id, "response")

            # 检查重复
            if cache_key in self._processed_ids:
                last_time = self._processed_ids[cache_key]
                if time.time() - last_time < 1.0:
                    logger.debug("[LLMDebugger] 跳过重复的响应记录: " + str(conv_id))
                    return

            # 获取响应文本
            text = ""
            try:
                if hasattr(resp, 'completion_text') and resp.completion_text:
                    text = resp.completion_text
                elif hasattr(resp, 'content') and resp.content:
                    text = resp.content
                elif hasattr(resp, 'message') and resp.message:
                    text = resp.message
                elif hasattr(resp, 'text') and resp.text:
                    text = resp.text
                elif isinstance(resp, str):
                    text = resp
                else:
                    text = str(resp)
            except Exception as e:
                text = str(resp)
                logger.debug("[LLMDebugger] 获取响应文本失败: " + str(e))

            # 获取模型信息
            model = None
            try:
                if hasattr(resp, 'model') and resp.model:
                    model = resp.model
            except:
                pass

            # 获取usage
            usage = None
            try:
                if hasattr(resp, 'usage') and resp.usage:
                    usage = self._make_serializable(resp.usage)
                elif hasattr(resp, 'get_usage') and callable(getattr(resp, 'get_usage')):
                    usage = self._make_serializable(resp.get_usage())
            except Exception as e:
                logger.debug("[LLMDebugger] 获取usage失败: " + str(e))

            # 获取原始响应
            raw = None
            try:
                if hasattr(resp, 'raw_completion') and resp.raw_completion:
                    raw = self._make_serializable(resp.raw_completion)
                elif hasattr(resp, 'get_raw') and callable(getattr(resp, 'get_raw')):
                    raw = self._make_serializable(resp.get_raw())
                elif hasattr(resp, 'raw') and resp.raw:
                    raw = self._make_serializable(resp.raw)
            except Exception as e:
                logger.debug("[LLMDebugger] 获取raw失败: " + str(e))

            data = {
                "timestamp": datetime.now().isoformat(),
                "type": "response",
                "conversation_id": conv_id,
                "sender": {
                    "id": event.get_sender_id() if event else "unknown", 
                    "name": event.get_sender_name() if event else "unknown"
                },
                "response": {
                    "text": text,
                    "model": model,
                    "raw": raw
                },
                "usage": usage
            }

            safe_data = self._make_serializable(data)
            record_id = await self.db.save_record(safe_data)
            safe_data["id"] = record_id
            await self._broadcast(safe_data)
            self._processed_ids[cache_key] = time.time()

            logger.info("[LLMDebugger] 已记录响应: conv_id=" + str(conv_id) + ", model=" + str(model) + ", text_length=" + str(len(text)))

        except Exception as e:
            logger.error("[LLMDebugger] 记录响应失败: " + str(e) + "\n" + traceback.format_exc())

        # 新增：同时触发抓包响应
        asyncio.create_task(self.capture_llm_response(event, resp))

    # ========== 工具方法 ==========
    def _make_serializable(self, obj):
        """将对象转换为可序列化的格式"""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (list, tuple, set)):
            return [self._make_serializable(item) for item in obj]
        if isinstance(obj, dict):
            return {key: self._make_serializable(value) for key, value in obj.items()}
        if hasattr(obj, 'model_dump'):
            return self._make_serializable(obj.model_dump())
        if hasattr(obj, 'dict'):
            return self._make_serializable(obj.dict())
        if hasattr(obj, '__dict__'):
            return self._make_serializable(obj.__dict__)
        return str(obj)

    async def _broadcast(self, data: Dict[str, Any]):
        """广播消息到所有WebSocket客户端"""
        if not self.connected_clients:
            return
        message = json.dumps(data, ensure_ascii=False, default=str)
        disconnected = set()
        for ws in self.connected_clients:
            try:
                await ws.send(message)
            except Exception:
                disconnected.add(ws)
        self.connected_clients -= disconnected

    def register_ws_client(self, ws):
        """注册WebSocket客户端"""
        self.connected_clients.add(ws)

    def unregister_ws_client(self, ws):
        """注销WebSocket客户端"""
        self.connected_clients.discard(ws)

    async def get_recent_records(self, limit=100):
        """获取最近的记录"""
        if self.db:
            return await self.db.get_recent_records(limit)
        return []

    async def terminate(self):
        """插件卸载"""
        logger.info("[LLMDebugger] 正在停止 Web 服务器...")
        if self._shutdown_event:
            self._shutdown_event.set()

        if self.web_server:
            try:
                await asyncio.wait_for(self._server_stopped.wait(), timeout=5.0)
                logger.debug("[LLMDebugger] Web服务器已正常停止")
            except asyncio.TimeoutError:
                logger.warning("[LLMDebugger] 等待服务器停止超时，将强制取消任务")
                self.web_server.cancel()
                try:
                    await self.web_server
                except asyncio.CancelledError:
                    pass

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # 关闭所有WebSocket连接（包括抓包客户端）
        for ws in list(self.connected_clients):
            try:
                await ws.close(1000)
            except:
                pass
        self.connected_clients.clear()

        # 新增：关闭抓包客户端连接
        for ws in list(self.capture_clients):
            try:
                await ws.close(1000)
            except:
                pass
        self.capture_clients.clear()

        logger.info("[LLMDebugger] 已停止")


# ========== 数据库类（原有功能） ==========
class Database:
    """数据库管理类（原有功能）"""

    def __init__(self, db_path: str, max_records: int = 5000):
        self.db_path = db_path
        self.max_records = max_records

    async def init(self):
        """初始化数据库"""
        import aiosqlite
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS llm_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, type TEXT, conversation_id TEXT,
                    sender_id TEXT, sender_name TEXT, data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON llm_records(timestamp DESC)')
            await db.commit()

    async def save_record(self, data: Dict[str, Any]) -> int:
        """保存记录"""
        import aiosqlite
        safe_data = self._make_serializable(data)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO llm_records (timestamp, type, conversation_id, sender_id, sender_name, data) 
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (safe_data.get('timestamp'), safe_data.get('type'), safe_data.get('conversation_id'),
                 safe_data.get('sender', {}).get('id'), safe_data.get('sender', {}).get('name'),
                 json.dumps(safe_data, ensure_ascii=False))
            )
            await db.commit()
            if self.max_records > 0:
                await self._cleanup(db)
            return cursor.lastrowid

    def _make_serializable(self, obj):
        """序列化对象"""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (list, tuple, set)):
            return [self._make_serializable(item) for item in obj]
        if isinstance(obj, dict):
            return {key: self._make_serializable(value) for key, value in obj.items()}
        if hasattr(obj, 'model_dump'):
            return self._make_serializable(obj.model_dump())
        if hasattr(obj, 'dict'):
            return self._make_serializable(obj.dict())
        if hasattr(obj, '__dict__'):
            return self._make_serializable(obj.__dict__)
        return str(obj)

    async def _cleanup(self, db):
        """清理旧记录"""
        cursor = await db.execute('SELECT COUNT(*) FROM llm_records')
        count = (await cursor.fetchone())[0]
        if count > self.max_records:
            to_delete = count - self.max_records
            await db.execute('DELETE FROM llm_records WHERE id IN (SELECT id FROM llm_records ORDER BY timestamp ASC LIMIT ?)', (to_delete,))
            await db.commit()

    async def get_recent_records(self, limit: int = 100):
        """获取最近记录"""
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT id, data FROM llm_records ORDER BY timestamp DESC LIMIT ?', (limit,))
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                data = json.loads(row['data'])
                if 'id' not in data:
                    data['id'] = row['id']
                result.append(data)
            return result


# ========== 新增：抓包专用数据库类 ==========
class CaptureDatabase:
    """抓包数据专用数据库（独立存储）"""

    def __init__(self, db_path: str, max_records: int = 1000):
        self.db_path = db_path
        self.max_records = max_records

    async def init(self):
        """初始化抓包数据库"""
        import aiosqlite
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            # 使用独立表结构，更侧重技术调试
            await db.execute("""
                CREATE TABLE IF NOT EXISTS packet_capture (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    phase TEXT,
                    conversation_id TEXT,
                    sender_id TEXT,
                    sender_name TEXT,
                    data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute('CREATE INDEX IF NOT EXISTS idx_capture_timestamp ON packet_capture(timestamp DESC)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_capture_conv ON packet_capture(conversation_id)')
            await db.commit()

    async def save_record(self, data: Dict[str, Any]) -> int:
        """保存抓包记录"""
        import aiosqlite
        safe_data = self._make_serializable(data)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO packet_capture (timestamp, phase, conversation_id, sender_id, sender_name, data) 
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (safe_data.get('timestamp'), 
                 safe_data.get('phase'), 
                 safe_data.get('conversation_id'),
                 safe_data.get('sender', {}).get('id'), 
                 safe_data.get('sender', {}).get('name'),
                 json.dumps(safe_data, ensure_ascii=False))
            )
            await db.commit()
            if self.max_records > 0:
                await self._cleanup(db)
            return cursor.lastrowid

    def _make_serializable(self, obj):
        """序列化对象"""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (list, tuple, set)):
            return [self._make_serializable(item) for item in obj]
        if isinstance(obj, dict):
            return {key: self._make_serializable(value) for key, value in obj.items()}
        if hasattr(obj, 'model_dump'):
            return self._make_serializable(obj.model_dump())
        if hasattr(obj, 'dict'):
            return self._make_serializable(obj.dict())
        if hasattr(obj, '__dict__'):
            return self._make_serializable(obj.__dict__)
        return str(obj)

    async def _cleanup(self, db):
        """清理旧记录"""
        cursor = await db.execute('SELECT COUNT(*) FROM packet_capture')
        count = (await cursor.fetchone())[0]
        if count > self.max_records:
            to_delete = count - self.max_records
            await db.execute(
                'DELETE FROM packet_capture WHERE id IN (SELECT id FROM packet_capture ORDER BY timestamp ASC LIMIT ?)', 
                (to_delete,)
            )
            await db.commit()

    async def get_recent_records(self, limit: int = 100):
        """获取最近抓包记录"""
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT id, data FROM packet_capture ORDER BY timestamp DESC LIMIT ?', 
                (limit,)
            )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                data = json.loads(row['data'])
                if 'id' not in data:
                    data['id'] = row['id']
                result.append(data)
            return result

    async def clear_all(self):
        """清空所有抓包数据"""
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM packet_capture')
            await db.execute('VACUUM')  # 收缩数据库文件
            await db.commit()