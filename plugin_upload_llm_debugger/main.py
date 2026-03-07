"""LLM Debugger

版本: 1.3.2
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


@register("llm_debugger", "韶虹CYun", "LLM 调用监控调试器（带WebUI）", "1.3.2")
class LLMDebugger(Star):
    """LLM 调用监控调试器（带WebUI）- 支持MoreChatPlus数据查看"""

    DEFAULT_PORT = 6188
    DEFAULT_PASSWORD = "llm_debugger1357"
    DEFAULT_MAX_RECORDS = 500

    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.db = None
        self.web_server = None
        self.connected_clients: Set = set()
        self._shutdown_event = None
        self._server_stopped = asyncio.Event()
        self._processed_ids: Dict[Tuple[Optional[str], str], float] = {}
        self._cleanup_task = None

        self._register_to_context()

    def _register_to_context(self):
        """将debugger实例注册到context，方便其他插件获取"""
        try:
            if not hasattr(self.context, '_plugin_instances'):
                self.context._plugin_instances = {}
            self.context._plugin_instances['llm_debugger'] = self
            logger.info("[LLMDebugger] 已注册到 context._plugin_instances")
        except Exception as e:
            logger.debug(f"[LLMDebugger] 注册到 _plugin_instances 失败: {e}")

        try:
            if hasattr(self.context, 'star_registry'):
                if isinstance(self.context.star_registry, dict):
                    self.context.star_registry['llm_debugger'] = self
                    logger.info("[LLMDebugger] 已注册到 context.star_registry")
        except Exception as e:
            logger.debug(f"[LLMDebugger] 注册到 star_registry 失败: {e}")

    async def initialize(self):
        """初始化插件"""
        try:
            import aiosqlite
            from quart import Quart, render_template, websocket, request, session, redirect, abort, render_template_string, jsonify
            from quart_cors import cors
            import functools
            import base64
        except ImportError as e:
            logger.error(f"[LLMDebugger] 缺少依赖库: {e}。请安装: pip install aiosqlite quart quart-cors")
            return

        plugin_config = self.context.get_config("llm_debugger") or {}
        port = plugin_config.get("port", self.DEFAULT_PORT)
        password = plugin_config.get("password", self.DEFAULT_PASSWORD)
        max_records = plugin_config.get("max_records", self.DEFAULT_MAX_RECORDS)

        logger.info(f"[LLMDebugger] 配置加载: port={port}, password={'已设置' if password else '未设置'}, max_records={max_records}")

        data_dir = "/AstrBot/data"
        if not os.path.exists(data_dir):
            data_dir = os.path.join(os.path.dirname(__file__), "data")
        db_dir = os.path.join(data_dir, "plugin_data", "llm_debugger")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "logs.db")

        self.db = Database(db_path, max_records)
        await self.db.init()

        self._cleanup_task = asyncio.create_task(self._cleanup_cache())
        self._server_stopped.clear()
        self._shutdown_event = asyncio.Event()
        self.web_server = asyncio.create_task(self._start_web_server(port, password))
        logger.info(f"[LLMDebugger] WebUI 已启动: http://0.0.0.0:{port}")

    async def _cleanup_cache(self):
        """定期清理去重缓存"""
        while True:
            await asyncio.sleep(3600)
            try:
                now = time.time()
                to_remove = [key for key, ts in self._processed_ids.items() if now - ts > 3600]
                for key in to_remove:
                    del self._processed_ids[key]
                logger.debug(f"[LLMDebugger] 清理了 {len(to_remove)} 条去重缓存记录")
            except Exception as e:
                logger.error(f"[LLMDebugger] 清理缓存失败: {e}")

    # ========== 公共方法：供其他插件手动记录 LLM 调用 ==========
    async def record_llm_call(self, data: dict):
        """供其他插件调用的公共方法，用于记录 LLM 请求或响应"""
        if not self.db:
            logger.debug("[LLMDebugger] 数据库未初始化，无法记录")
            return

        try:
            phase = data.get("phase")
            if phase not in ("request", "response"):
                logger.warning(f"[LLMDebugger] 忽略无效 phase: {phase}")
                return

            record_data = {
                "timestamp": data.get("timestamp", datetime.now().isoformat()),
                "type": phase,
                "conversation_id": data.get("conversation_id"),
                "sender": data.get("sender", {}),
                "source": data.get("source", {}),
            }

            if phase == "request":
                record_data.update({
                    "message": {
                        "raw": data.get("prompt", ""),
                        "formatted_prompt": data.get("prompt", ""),
                        "image_urls": data.get("images", []),
                    },
                    "llm_config": {
                        "model": data.get("model"),
                        "system_prompt": data.get("system_prompt", ""),
                        "contexts": data.get("contexts", []),
                        "contexts_count": len(data.get("contexts", [])),
                    }
                })
            else:
                record_data.update({
                    "response": {
                        "text": data.get("response", ""),
                        "model": data.get("model"),
                        "raw": data.get("raw_response"),
                    },
                    "usage": data.get("usage"),
                })

            safe_data = self._make_serializable(record_data)
            record_id = await self.db.save_record(safe_data)
            safe_data["id"] = record_id
            await self._broadcast(safe_data)

            conv_id = data.get("conversation_id")
            cache_key = (conv_id, phase)
            self._processed_ids[cache_key] = time.time()

            source_info = data.get('source', {})
            logger.debug(f"[LLMDebugger] 已记录 {phase} 来自 {source_info.get('plugin', 'unknown')}/{source_info.get('purpose', 'unknown')}")

        except Exception as e:
            logger.error(f"[LLMDebugger] record_llm_call 失败: {e}\n{traceback.format_exc()}")

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
                            """SELECT * FROM messages WHERE origin = ? 
                               ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
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
                            """SELECT * FROM context_summaries WHERE origin = ? 
                               ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
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
                    return {"error": f"未知查询类型: {query_type}"}

        except Exception as e:
            logger.error(f"[LLMDebugger] 查询MoreChatPlus数据库失败: {e}")
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
            logger.error(f"[LLMDebugger] 模板文件夹不存在: {template_dir}")
            return

        app = Quart(__name__, template_folder=template_dir)
        app = cors(app)
        app.secret_key = os.urandom(24)

        @app.errorhandler(Exception)
        async def handle_exception(e):
            if isinstance(e, NotFound):
                path = request.path
                logger.debug(f"[LLMDebugger] 404 Not Found: {path}")
                return "Not Found", 404
            logger.error(f"[LLMDebugger] 未捕获的异常: {e}\n{traceback.format_exc()}")
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
                    if request.path.startswith("/api/") or request.path == "/ws":
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

        @app.route("/logout")
        async def logout():
            session.pop("authenticated", None)
            return redirect("/login")

        @app.route("/")
        @require_auth
        async def index():
            try:
                return await render_template("index.html")
            except Exception as e:
                logger.error(f"[LLMDebugger] 渲染首页失败: {e}\n{traceback.format_exc()}")
                return "Internal Server Error", 500

        @app.route("/api/recent")
        @require_auth
        async def get_recent():
            try:
                records = await self.get_recent_records(100)
                return jsonify(records)
            except Exception as e:
                logger.error(f"[LLMDebugger] 获取最近记录失败: {e}\n{traceback.format_exc()}")
                return jsonify({"error": str(e)}), 500

        # ===== MoreChatPlus 数据查看 API =====
        @app.route("/api/morechatplus/stats")
        @require_auth
        async def get_morechatplus_stats():
            """获取MoreChatPlus统计信息"""
            try:
                stats = await self._query_morechatplus_db("stats")
                return jsonify(stats)
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/morechatplus/origins")
        @require_auth
        async def get_morechatplus_origins():
            """获取所有群聊来源"""
            try:
                origins = await self._query_morechatplus_db("origins")
                return jsonify({"origins": origins})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/morechatplus/messages")
        @require_auth
        async def get_morechatplus_messages():
            """获取消息记录"""
            try:
                args = request.args
                limit = min(int(args.get("limit", 50)), 100)
                offset = int(args.get("offset", 0))
                origin = args.get("origin")
                messages = await self._query_morechatplus_db("messages", limit, offset, origin)
                return jsonify({"messages": messages, "count": len(messages)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/morechatplus/profiles")
        @require_auth
        async def get_morechatplus_profiles():
            """获取用户画像"""
            try:
                args = request.args
                origin = args.get("origin")
                profiles = await self._query_morechatplus_db("user_profiles", origin=origin)
                return jsonify({"profiles": profiles, "count": len(profiles)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/morechatplus/summaries")
        @require_auth
        async def get_morechatplus_summaries():
            """获取上下文总结"""
            try:
                args = request.args
                limit = min(int(args.get("limit", 50)), 100)
                offset = int(args.get("offset", 0))
                origin = args.get("origin")
                summaries = await self._query_morechatplus_db("summaries", limit, offset, origin)
                return jsonify({"summaries": summaries, "count": len(summaries)})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

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
                logger.debug(f"[LLMDebugger] WebSocket 客户端断开: {e}")
            finally:
                self.unregister_ws_client(ws_obj)

        try:
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
            logger.error(f"[LLMDebugger] Web 服务器运行失败: {e}\n{traceback.format_exc()}")
        finally:
            self._server_stopped.set()

    # ========== 事件监听 ==========
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """监听LLM请求"""
        if not self.db:
            return
        try:
            conv_id = getattr(req, 'conversation_id', None) or getattr(req, 'session_id', None) or getattr(req, 'id', None)
            cache_key = (conv_id, "request")
            if cache_key in self._processed_ids:
                logger.debug(f"[LLMDebugger] 跳过重复的请求记录: {conv_id}")
                return

            prompt = getattr(req, 'prompt', '') or event.message_str
            contexts = getattr(req, 'contexts', []) or (req.get_contexts() if hasattr(req, 'get_contexts') and callable(req.get_contexts) else [])
            system_prompt = getattr(req, 'system_prompt', '') or (req.get_system_prompt() if hasattr(req, 'get_system_prompt') and callable(req.get_system_prompt) else '')
            model = getattr(req, 'model', 'default')
            image_urls = getattr(req, 'image_urls', []) or (req.get_image_urls() if hasattr(req, 'get_image_urls') and callable(req.get_image_urls) else [])

            data = {
                "timestamp": datetime.now().isoformat(),
                "type": "request",
                "conversation_id": conv_id,
                "sender": {
                    "id": event.get_sender_id(),
                    "name": event.get_sender_name(),
                    "group_id": getattr(event, 'get_group_id', lambda: None)() if hasattr(event, 'get_group_id') else None,
                    "platform": event.get_platform_name()
                },
                "message": {
                    "raw": event.message_str,
                    "formatted_prompt": prompt,
                    "image_urls": self._make_serializable(image_urls)
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
            logger.debug(f"[LLMDebugger] 已记录请求 {conv_id}")
        except Exception as e:
            logger.error(f"[LLMDebugger] 记录请求失败: {e}\n{traceback.format_exc()}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """监听LLM响应"""
        if not self.db:
            return
        try:
            conv_id = getattr(resp, 'conversation_id', None) or getattr(resp, 'session_id', None) or getattr(resp, 'id', None)
            cache_key = (conv_id, "response")
            if cache_key in self._processed_ids:
                logger.debug(f"[LLMDebugger] 跳过重复的响应记录: {conv_id}")
                return

            text = getattr(resp, 'completion_text', '') or getattr(resp, 'content', '') or getattr(resp, 'message', '') or getattr(resp, 'text', '') or (resp if isinstance(resp, str) else str(resp))
            model = getattr(resp, 'model', None)
            usage = getattr(resp, 'usage', None) or (resp.get_usage() if hasattr(resp, 'get_usage') and callable(resp.get_usage) else None)
            raw = getattr(resp, 'raw_completion', None) or (resp.get_raw() if hasattr(resp, 'get_raw') and callable(resp.get_raw) else None)

            if usage is not None:
                usage = self._make_serializable(usage)
            if raw is not None:
                raw = self._make_serializable(raw)

            data = {
                "timestamp": datetime.now().isoformat(),
                "type": "response",
                "conversation_id": conv_id,
                "sender": {"id": event.get_sender_id(), "name": event.get_sender_name()},
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
            logger.debug(f"[LLMDebugger] 已记录响应 {conv_id}")
        except Exception as e:
            logger.error(f"[LLMDebugger] 记录响应失败: {e}\n{traceback.format_exc()}")

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

        for ws in list(self.connected_clients):
            try:
                await ws.close(1000)
            except:
                pass
        self.connected_clients.clear()

        logger.info("[LLMDebugger] 已停止")


# ========== 数据库类 ==========
class Database:
    """数据库管理类"""

    def __init__(self, db_path: str, max_records: int = 5000):
        self.db_path = db_path
        self.max_records = max_records

    async def init(self):
        """初始化数据库"""
        import aiosqlite
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS llm_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, type TEXT, conversation_id TEXT,
                    sender_id TEXT, sender_name TEXT, data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON llm_records(timestamp DESC)')
            await db.commit()

    async def save_record(self, data: Dict[str, Any]) -> int:
        """保存记录"""
        import aiosqlite
        safe_data = self._make_serializable(data)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                '''INSERT INTO llm_records (timestamp, type, conversation_id, sender_id, sender_name, data) 
                   VALUES (?, ?, ?, ?, ?, ?)''',
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