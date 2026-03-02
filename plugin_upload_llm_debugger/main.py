import os
import json
import asyncio
import traceback
from datetime import datetime
from typing import Dict, Any, Set

from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

# 兼容 Provider 导入（仅用于类型提示，非必需）
try:
    from astrbot.api.provider import ProviderRequest, LLMResponse
except ImportError:
    ProviderRequest = None
    LLMResponse = None


@register("llm_debugger", "韶虹CYun", "LLM 调用监控调试器（带WebUI）", "1.0.0")
class LLMDebugger(Star):
    """LLM 调用监控调试器（带WebUI）"""

    # 默认配置（与 _conf_schema.json 保持一致）
    DEFAULT_PORT = 6188
    DEFAULT_PASSWORD = "llm_debugger1357"
    DEFAULT_MAX_RECORDS = 500

    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.db = None
        self.web_server = None
        self.connected_clients: Set = set()

    async def initialize(self):
        """初始化数据库和 Web 服务器"""
        try:
            import aiosqlite
            from quart import Quart, render_template, websocket, request, session, redirect, abort, render_template_string, jsonify
            from quart_cors import cors
            import functools
            import base64
        except ImportError as e:
            logger.error(f"[LLMDebugger] 缺少依赖库: {e}。请安装: pip install aiosqlite quart quart-cors")
            return

        # 读取插件配置
        plugin_config = self.context.get_config("llm_debugger") or {}
        port = plugin_config.get("port", self.DEFAULT_PORT)
        password = plugin_config.get("password", self.DEFAULT_PASSWORD)
        max_records = plugin_config.get("max_records", self.DEFAULT_MAX_RECORDS)

        logger.info(f"[LLMDebugger] 配置加载: port={port}, password={'已设置' if password else '未设置'}, max_records={max_records}")

        # 准备数据目录
        data_dir = "/AstrBot/data"
        if not os.path.exists(data_dir):
            data_dir = os.path.join(os.path.dirname(__file__), "data")
        db_dir = os.path.join(data_dir, "plugin_data", "llm_debugger")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "logs.db")

        # 数据库类（修改 get_recent_records 以包含 id）
        class Database:
            def __init__(self, db_path: str, max_records: int = 5000):
                self.db_path = db_path
                self.max_records = max_records

            async def init(self):
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
                # 在保存前确保 data 完全可序列化
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
                """递归将对象转换为 JSON 可序列化的基本类型"""
                if obj is None or isinstance(obj, (str, int, float, bool)):
                    return obj
                if isinstance(obj, (list, tuple, set)):
                    return [self._make_serializable(item) for item in obj]
                if isinstance(obj, dict):
                    return {key: self._make_serializable(value) for key, value in obj.items()}
                # 处理常见复杂对象
                if hasattr(obj, 'model_dump'):  # Pydantic v2
                    return self._make_serializable(obj.model_dump())
                if hasattr(obj, 'dict'):        # Pydantic v1
                    return self._make_serializable(obj.dict())
                if hasattr(obj, '__dict__'):     # 普通对象
                    return self._make_serializable(obj.__dict__)
                # 兜底：转换为字符串
                return str(obj)

            async def _cleanup(self, db):
                cursor = await db.execute('SELECT COUNT(*) FROM llm_records')
                count = (await cursor.fetchone())[0]
                if count > self.max_records:
                    to_delete = count - self.max_records
                    await db.execute('DELETE FROM llm_records WHERE id IN (SELECT id FROM llm_records ORDER BY timestamp ASC LIMIT ?)', (to_delete,))
                    await db.commit()

            async def get_recent_records(self, limit: int = 100):
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    # 同时查询 id 和 data，确保返回的数据中包含 id 字段
                    cursor = await db.execute('SELECT id, data FROM llm_records ORDER BY timestamp DESC LIMIT ?', (limit,))
                    rows = await cursor.fetchall()
                    result = []
                    for row in rows:
                        data = json.loads(row['data'])
                        # 如果 data 中没有 id（旧记录），则使用数据库的 id
                        if 'id' not in data:
                            data['id'] = row['id']
                        result.append(data)
                    return result

        self.db = Database(db_path, max_records)
        await self.db.init()

        # 启动 Web 服务器
        self.web_server = asyncio.create_task(self._start_web_server(port, password))
        logger.info(f"[LLMDebugger] WebUI 已启动: http://0.0.0.0:{port} (密码: {'已启用' if password else '无'})")

    async def _start_web_server(self, port: int, password: str):
        """创建并运行 Quart 应用（增强错误处理）"""
        from quart import Quart, render_template, websocket, request, session, redirect, abort, render_template_string, jsonify
        from quart_cors import cors
        import functools
        import base64

        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        if not os.path.isdir(template_dir):
            logger.error(f"[LLMDebugger] 模板文件夹不存在: {template_dir}")
            return

        app = Quart(__name__, template_folder=template_dir)
        app = cors(app)
        app.secret_key = os.urandom(24)

        @app.errorhandler(Exception)
        async def handle_exception(e):
            logger.error(f"[LLMDebugger] 未捕获的异常: {e}\n{traceback.format_exc()}")
            return "Internal Server Error", 500

        # 认证辅助函数（略，与之前相同）...
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
            await app.run_task(host='0.0.0.0', port=port, debug=False)
        except Exception as e:
            logger.error(f"[LLMDebugger] Web 服务器启动失败: {e}\n{traceback.format_exc()}")

    # ---------- 辅助函数：将数据递归转换为可 JSON 序列化的格式 ----------
    def _make_serializable(self, obj):
        """递归将对象转换为 JSON 可序列化的基本类型"""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (list, tuple, set)):
            return [self._make_serializable(item) for item in obj]
        if isinstance(obj, dict):
            return {key: self._make_serializable(value) for key, value in obj.items()}
        # 处理常见复杂对象
        if hasattr(obj, 'model_dump'):  # Pydantic v2
            return self._make_serializable(obj.model_dump())
        if hasattr(obj, 'dict'):        # Pydantic v1
            return self._make_serializable(obj.dict())
        if hasattr(obj, '__dict__'):     # 普通对象
            return self._make_serializable(obj.__dict__)
        # 兜底：转换为字符串
        return str(obj)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """记录 LLM 请求（增强数据提取）"""
        if not self.db:
            return
        try:
            logger.debug(f"[LLMDebugger] on_llm_request 触发，req 类型: {type(req)}")
            logger.debug(f"req dir: {dir(req)}")

            # 尝试提取 conversation_id
            conv_id = getattr(req, 'conversation_id', None)
            if conv_id is None:
                conv_id = getattr(req, 'session_id', None) or getattr(req, 'id', None)

            # 提取 prompt
            prompt = getattr(req, 'prompt', '')
            if not prompt:
                if hasattr(req, 'get_prompt') and callable(req.get_prompt):
                    prompt = req.get_prompt()
                else:
                    prompt = event.message_str

            # 提取 contexts（可能是对话历史）
            contexts = getattr(req, 'contexts', [])
            if not contexts and hasattr(req, 'get_contexts') and callable(req.get_contexts):
                contexts = req.get_contexts()

            # 提取 system_prompt
            system_prompt = getattr(req, 'system_prompt', '')
            if not system_prompt and hasattr(req, 'get_system_prompt') and callable(req.get_system_prompt):
                system_prompt = req.get_system_prompt()

            # 提取 model
            model = getattr(req, 'model', 'default')

            # 提取 image_urls
            image_urls = getattr(req, 'image_urls', [])
            if not image_urls and hasattr(req, 'get_image_urls') and callable(req.get_image_urls):
                image_urls = req.get_image_urls()

            # 构建数据字典，并确保所有字段可序列化
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
                    "contexts": self._make_serializable(contexts)  # 转换 contexts
                }
            }

            # 最后确保整个 data 可序列化
            safe_data = self._make_serializable(data)
            record_id = await self.db.save_record(safe_data)
            safe_data["id"] = record_id
            await self._broadcast(safe_data)
            logger.info(f"[LLMDebugger] 已记录请求 {conv_id}")
        except Exception as e:
            logger.error(f"[LLMDebugger] 记录请求失败: {e}")
            logger.error(traceback.format_exc())

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """记录 LLM 响应（增强数据提取）"""
        if not self.db:
            return
        try:
            logger.debug(f"[LLMDebugger] on_llm_response 触发，resp 类型: {type(resp)}")
            logger.debug(f"resp dir: {dir(resp)}")

            # 提取 conversation_id
            conv_id = getattr(resp, 'conversation_id', None)
            if conv_id is None:
                conv_id = getattr(resp, 'session_id', None) or getattr(resp, 'id', None)

            # 提取 completion_text
            text = getattr(resp, 'completion_text', '')
            if not text:
                text = getattr(resp, 'content', '') or getattr(resp, 'message', '') or getattr(resp, 'text', '')
                if not text:
                    if isinstance(resp, str):
                        text = resp
                    else:
                        text = str(resp)

            # 提取 model
            model = getattr(resp, 'model', None)

            # 提取 usage
            usage = getattr(resp, 'usage', None)
            if usage is None and hasattr(resp, 'get_usage') and callable(resp.get_usage):
                usage = resp.get_usage()
            # 转换 usage 为可序列化格式
            if usage is not None:
                usage = self._make_serializable(usage)

            # 提取 raw_completion
            raw = getattr(resp, 'raw_completion', None)
            if raw is None and hasattr(resp, 'get_raw') and callable(resp.get_raw):
                raw = resp.get_raw()
            # 转换 raw 为可序列化格式
            if raw is not None:
                raw = self._make_serializable(raw)

            # 构建数据字典
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

            # 确保整个 data 可序列化
            safe_data = self._make_serializable(data)
            record_id = await self.db.save_record(safe_data)
            safe_data["id"] = record_id
            await self._broadcast(safe_data)
            logger.info(f"[LLMDebugger] 已记录响应 {conv_id}")
        except Exception as e:
            logger.error(f"[LLMDebugger] 记录响应失败: {e}")
            logger.error(traceback.format_exc())

    async def _broadcast(self, data: Dict[str, Any]):
        """广播到 WebSocket 客户端"""
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
        self.connected_clients.add(ws)

    def unregister_ws_client(self, ws):
        self.connected_clients.discard(ws)

    async def get_recent_records(self, limit=100):
        if self.db:
            return await self.db.get_recent_records(limit)
        return []

    async def terminate(self):
        """插件卸载时清理"""
        if self.web_server:
            self.web_server.cancel()
            try:
                await self.web_server
            except asyncio.CancelledError:
                pass
        logger.info("[LLMDebugger] 已停止")