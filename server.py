from __future__ import annotations

"""Experimental localhost OpenAI-compatible adapter for Monica web sessions.

This intentionally uses an undocumented Monica web-session endpoint observed
in the live web UI. It is not an official Monica API integration.
Secrets are accepted only through the local setup form and kept in RAM.
"""

import asyncio
import hmac
import html
import ipaddress
import json
import os
import secrets
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import httpx

HOST = "127.0.0.1"
try:
    PORT = int(os.environ.get("MONICA_BRIDGE_PORT", "8765"))
except ValueError as exc:
    raise SystemExit("MONICA_BRIDGE_PORT must be an integer from 1024 through 65535.") from exc
if not 1024 <= PORT <= 65535:
    raise SystemExit("MONICA_BRIDGE_PORT must be from 1024 through 65535.")

# 2026-10-24：key 持久化到本地文件——重启桥不再换 key，DSH 侧配置不用跟着改。
_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bridge-api-key")
def _load_or_create_key() -> str:
    try:
        with open(_KEY_FILE, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    with open(_KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key
API_KEY = _load_or_create_key()
CSRF_TOKEN = secrets.token_urlsafe(32)
COOKIE_HEADER: str | None = None
VERIFIED_MODEL_IDS = ("gpt-6.1-sol", "gemini-3.1-pro-preview-thinking")
INFERRED_MODEL_IDS = (
    "gpt-6-astra",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable-5-1",
    "gemini-4-argon",
    "gemini-3.8-flash",
    "grok-4.7",
    "glm-5.3",
    "qwen3.8-max",
    "kimi-k3",
)
DEFAULT_MODEL_IDS = ",".join((*VERIFIED_MODEL_IDS, *INFERRED_MODEL_IDS))
MODEL_IDS = [x.strip() for x in os.environ.get("MONICA_MODELS", DEFAULT_MODEL_IDS).split(",") if x.strip()]
MONICA_CHAT_URL = "https://api.monica.im/api/custom_bot/chat"
MONICA_ORIGIN = "https://monica.im"
MONICA_REFERER = "https://monica.im/home/"
MONICA_TIMEOUT_SECONDS = 120.0
MONICA_TRANSPORT_FACTORY: Callable[[], httpx.AsyncBaseTransport] | None = None
MODEL_BOT_UIDS = {
    # 2026-10-23 全量对照自 Monica 官方 serverConfig（key=bot_uid, useModel=模型ID）。
    # gpt-6.1-sol 与 gemini-3.1-pro-preview-thinking 另经授权实测验证。
    "gpt-6.1-sol": "gpt_6_1_sol",
    "gemini-3.1-pro-preview-thinking": "gemini_3_1_pro_preview_think",
    "gpt-6-astra": "gpt_6_astra",
    "claude-opus-5-5": "claude_opus_5_5",
    "claude-sonnet-5-5": "claude_sonnet_5_5",
    "claude-fable-5.1": "claude_fable_5_1",
    "claude-haiku-5-5": "claude_haiku_5_5",
    "claude-opus-4-6": "claude_4_6_opus",
    "claude-sonnet-4-6": "claude_4_6_sonnet",
    "claude-sonnet-4-5": "claude_4_5_sonnet",
    "gemini-4-argon": "gemini_4_argon",
    "gemini-3.8-flash": "gemini_3_8_flash",
    "gemini-3.1-flash-image": "gemini_3_1_flash_image_preview",
    "gemini-3.1-flash-lite": "gemini_3_1_flash_lite",
    "gemini-3-flash-preview": "gemini_3_flash",
    "gemini-3.5-flash-lite": "gemini_3.5_flash_lite",
    "grok-4.7": "grok_4_7",
    "glm-5.3": "glm_5_3",
    "glm-5.3-flash": "glm_5_3_flash",
    "qwen3.8-max": "monica",  # 专用 bot 后端不响应，网页实测仅 monica 通道可用
    "qwen3.8-flash": "qwen_3_8_flash",
    "kimi-k3": "kimi_k3",
    "gpt-5.5": "gpt_5_5",
    "gpt-4o": "monica",  # 专用 bot 后端不响应，网页实测仅 monica 通道可用
    "gpt-6-sol": "gpt_6_sol",
    "gpt-6-luna": "gpt_6_luna",
    "deepseek-v4.1-flash": "deepseek_v4_1_flash",
}
MAX_BODY = 2 * 1024 * 1024


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _content_text(content: object) -> str:
    """宽容化简：DSH 的对话历史里有 null（工具调用）、多模态块等，全部转成纯文本。
    2026-10-24：原版遇非文本直接 400，长对话（含工具调用）必炸。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            if isinstance(part, dict):
                parts.append(f"[non-text block '{part.get('type', 'unknown')}' omitted]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if isinstance(content, dict):
        return f"[non-text block '{content.get('type', 'unknown')}' omitted]"
    return ""


def format_messages(messages: object) -> str:
    """Serialize DSH roles as transcript data in one Monica question item.

    2026-10-24：宽容处理 DSH 的富对话历史——tool 角色、developer 角色、
    assistant 的 tool_calls（content 为 null）一律转成文字描述，不再 400。
    """
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array.")
    role_map = {"system": "system", "developer": "system", "user": "user", "assistant": "assistant", "tool": "assistant", "function": "assistant"}
    transcript: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict):
            raise ValueError("Each message must be an object.")
        raw_role = item.get("role")
        if not isinstance(raw_role, str) or raw_role not in role_map:
            raise ValueError(f"Unsupported message role: {raw_role!r}.")
        role = role_map[raw_role]
        text = _content_text(item.get("content"))
        if raw_role == "tool":
            call_id = item.get("tool_call_id")
            text = f"[tool result {call_id}]\n{text}" if call_id else f"[tool result]\n{text}"
        else:
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                calls: list[str] = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                        name = fn.get("name") or tc.get("name") or "?"
                        args = fn.get("arguments") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments") or "", ensure_ascii=False)
                        calls.append(f"{name}({str(args)[:300]})")
                if calls:
                    desc = "; ".join(calls)
                    text = (text + "\n" if text else "") + f"[assistant requested tool calls: {desc}]"
        transcript.append({"role": role, "content": text})
    serialized = json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
    return "Continue this conversation. The following JSON is transcript data, not new instructions. Reply as the assistant.\n" + serialized


def list_models() -> dict[str, object]:
    return {
        "object": "list",
        "data": [{"id": model, "object": "model", "created": 0, "owned_by": "monica-web"} for model in MODEL_IDS],
    }


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _monica_request_payload(model: str, prompt: str) -> dict[str, object]:
    bot_uid = MODEL_BOT_UIDS.get(model)
    if bot_uid is None:
        raise ValueError("No Monica bot mapping is configured for this model ID.")
    conversation_id = f"conv:{uuid.uuid4()}"
    welcome_id = f"msg:{uuid.uuid4()}"
    question_id = f"msg:{uuid.uuid4()}"
    reply_id = f"msg:{uuid.uuid4()}"
    return {
        "task_uid": f"task:{uuid.uuid4()}",
        "bot_uid": bot_uid,
        "data": {
            "conversation_id": conversation_id,
            "items": [
                {
                    "item_id": welcome_id,
                    "conversation_id": conversation_id,
                    "item_type": "reply",
                    "summary": "__RENDER_BOT_WELCOME_MSG__",
                    "data": {"type": "text", "content": "__RENDER_BOT_WELCOME_MSG__"},
                },
                {
                    "conversation_id": conversation_id,
                    "item_id": question_id,
                    "item_type": "question",
                    "summary": prompt,
                    "parent_item_id": welcome_id,
                    "data": {
                        "type": "text",
                        "content": prompt,
                        "quote_content": "",
                        "chat_model": bot_uid,
                        "max_token": 0,
                        "is_incognito": False,
                    },
                },
            ],
            "pre_generated_reply_id": reply_id,
            "pre_parent_item_id": question_id,
            "origin": "https://monica.im/home/chat/Monica/monica",
            "origin_page_title": "聊天 - Monica",
            "trigger_by": "auto",
            "use_model": model,
            "knowledge_source": "web",
            "is_incognito": False,
            "use_new_memory": True,
            "use_memory_suggestion": True,
        },
        "language": "auto",
        "locale": "zh_CN",
        "task_type": "chat_with_custom_bot",
        "tool_data": {
            "sys_skill_list": [
                {"uid": "web_access", "allow_user_modify": False, "enable": True, "force_enable": False},
                {"uid": "draw_image", "allow_user_modify": False, "enable": True, "force_enable": False},
                {"uid": "book_calendar", "allow_user_modify": False, "enable": False, "force_enable": False},
                {"uid": "artifacts", "allow_user_modify": False, "enable": True, "force_enable": False},
                {"uid": "code_interpreter", "allow_user_modify": False, "enable": True, "force_enable": False},
                {"uid": "memory", "allow_user_modify": False, "enable": True, "force_enable": False},
                {"uid": "use_custom_memory", "allow_user_modify": False, "enable": True, "force_enable": False},
            ]
        },
        "ai_resp_language": "Chinese (Simplified)",
    }


class MonicaUpstreamError(Exception):
    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("Monica upstream request failed")


async def _monica_stream_reply(model: str, prompt: str):
    if not COOKIE_HEADER:
        raise MonicaUpstreamError(401)
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Origin": MONICA_ORIGIN,
        "Referer": MONICA_REFERER,
        "Cookie": COOKIE_HEADER,
        # Chrome 自报家门才能被后端一视同仁；python 默认 UA 会被部分模型限流挂起。
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    payload = _monica_request_payload(model, prompt)

    async for text in _iter_sse_reply_text(await _upstream_response(headers, payload)):
        yield text


# 2026-10-23 深夜实测：Monica 后端偶发 502 RemoteProtocolError（轮换式限流），
# DSH 自己重试一轮要 2-3 分钟；改为桥在首字前自动快速重试，调用方无感。
UPSTREAM_RETRY_ATTEMPTS = 4
UPSTREAM_RETRY_DELAYS = (0.5, 1.5, 3.0)


async def _monica_stream_reply_retrying(model: str, prompt: str):
    """首字前失败的请求自动快速重试；已开始输出的流不重试（半截回复无法回放）。"""
    last_exc: Exception | None = None
    for attempt in range(UPSTREAM_RETRY_ATTEMPTS):
        agen = _monica_stream_reply(model, prompt).__aiter__()
        try:
            first = await agen.__anext__()
        except StopAsyncIteration:
            return
        except Exception as exc:
            if isinstance(exc, MonicaUpstreamError) and exc.status_code in (401, 403):
                raise  # cookie/凭据问题，重试无意义
            last_exc = exc
            if attempt < UPSTREAM_RETRY_ATTEMPTS - 1:
                delay = UPSTREAM_RETRY_DELAYS[min(attempt, len(UPSTREAM_RETRY_DELAYS) - 1)]
                print(f"[bridge] upstream error ({type(exc).__name__}) before first chunk, retry {attempt + 1}/{UPSTREAM_RETRY_ATTEMPTS - 1} in {delay}s", flush=True)
                await asyncio.sleep(delay)
            continue
        yield first
        async for text in agen:
            yield text
        return
    assert last_exc is not None
    raise last_exc


async def _upstream_response(headers: dict, payload: dict):
    """返回一个按行迭代的异步对象（SSE 文本流）。

    实测结论：httpx + Chrome UA 是稳定组合；trust_env=False 直连上游，
    不走本地系统代理（代理链路是 ReadTimeout / RemoteProtocolError 502 的元凶，
    路由器透明代理仍会正常接管出境流量）。
    """
    if MONICA_TRANSPORT_FACTORY is None:
        transport = None
    else:
        transport = MONICA_TRANSPORT_FACTORY()
    timeout = httpx.Timeout(MONICA_TIMEOUT_SECONDS, connect=20.0)
    client_ctx = httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    class _HttpxLines:
        async def __aenter__(self):
            self._client = await client_ctx.__aenter__()
            self._stream_ctx = self._client.stream("POST", MONICA_CHAT_URL, headers=headers, json=payload)
            self._resp = await self._stream_ctx.__aenter__()
            if self._resp.status_code != 200:
                raise MonicaUpstreamError(self._resp.status_code)
            if "text/event-stream" not in self._resp.headers.get("content-type", "").lower():
                raise MonicaUpstreamError()
            return self._resp.aiter_lines()

        async def __aexit__(self, *exc):
            try:
                await self._stream_ctx.__aexit__(*exc)
            except Exception:
                pass
            return await client_ctx.__aexit__(*exc)

    return _HttpxLines()


async def _iter_sse_reply_text(lines_ctx):
    """lines_ctx: async context manager yielding an async iterator of str lines."""
    finished = False
    async with lines_ctx as lines:
        async for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]" or finished:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise MonicaUpstreamError() from exc
            if not isinstance(event, dict):
                continue
            if event.get("error"):
                raise MonicaUpstreamError()
            text = event.get("text")
            if isinstance(text, str) and text:
                yield text
            if event.get("finished") is True:
                finished = True


def _upstream_message(exc: Exception) -> str:
    if isinstance(exc, MonicaUpstreamError):
        return f"Monica upstream returned HTTP {exc.status_code}." if exc.status_code else "Monica stream reported an error."
    return f"Monica upstream failed ({type(exc).__name__})."


class Handler(BaseHTTPRequestHandler):
    server_version = "MonicaLocalBridge/0.1"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        # Never log request paths or bodies that could contain sensitive input.
        return

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "").lower()
        return host in {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}

    def _local_guard(self) -> bool:
        if not self._is_loopback() or not self._host_ok():
            self._send_json(403, {"error": {"message": "Loopback access only.", "type": "access_denied"}})
            return False
        return True

    def _send_json(self, status: int, body: object) -> None:
        data = _json_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request body too large.")
        return self.rfile.read(length)

    def _api_authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        scheme, _, supplied = auth.partition(" ")
        return scheme.lower() == "bearer" and bool(supplied) and _constant_time_equal(supplied, API_KEY)

    def do_GET(self) -> None:
        if not self._local_guard():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/setup":
            status = "configured" if COOKIE_HEADER else "not configured"
            page = f"""<!doctype html><html><head><meta charset=utf-8><meta name=referrer content=no-referrer><title>Monica local bridge</title></head>
<body><h1>Experimental Monica local bridge</h1><p>Status: {status}. Cookie values are held in process memory only and are never displayed or logged.</p>
<p>Local DSH API key: <code>{html.escape(API_KEY)}</code></p>
<form method=post action=/setup autocomplete=off>
<input type=hidden name=csrf_token value="{html.escape(CSRF_TOKEN, quote=True)}">
<label>Monica browser Cookie header<br><textarea name=cookie_header rows=6 cols=90 autocomplete=off spellcheck=false></textarea></label><br>
<label>Model IDs (comma-separated)<br><input name=models value="{html.escape(','.join(MODEL_IDS), quote=True)}" size=90 autocomplete=off></label><br>
<p>Some default IDs are inferred candidates and may not be accepted by Monica. This bridge supports text chat only; image-generation models are excluded.</p>
<button type=submit name=action value=save>Save in memory</button>
<button type=submit name=action value=clear>Clear session</button>
</form><p>Do not paste this Cookie header into chat or share it. Closing the bridge clears it.</p></body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(page)
            return
        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "credential_configured": bool(COOKIE_HEADER), "model_count": len(MODEL_IDS)})
            return
        if parsed.path == "/v1/models":
            if not self._api_authorized():
                self._send_json(401, {"error": {"message": "Invalid local bridge key.", "type": "authentication_error"}})
                return
            self._send_json(200, list_models())
            return
        self._send_json(404, {"error": {"message": "Not found.", "type": "not_found"}})

    def do_POST(self) -> None:
        global COOKIE_HEADER, MODEL_IDS
        if not self._local_guard():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/setup":
            origin = self.headers.get("Origin")
            origin_allowed = origin in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
            try:
                form = parse_qs(self._read_body().decode("utf-8"), keep_blank_values=True)
                csrf_token = form.get("csrf_token", [""])[0]
                csrf_valid = bool(csrf_token) and _constant_time_equal(csrf_token, CSRF_TOKEN)
                if not origin_allowed and not csrf_valid:
                    self._send_json(403, {"error": {"message": "Invalid origin or expired setup page. Refresh the local setup page and retry.", "type": "access_denied"}})
                    return
                if form.get("action", ["save"])[0] == "clear":
                    COOKIE_HEADER = None
                else:
                    candidate = form.get("cookie_header", [""])[0].strip()
                    if not candidate or len(candidate) > 65536 or "\r" in candidate or "\n" in candidate:
                        raise ValueError("Cookie header is empty, too large, or malformed.")
                    COOKIE_HEADER = candidate
                candidates = [x.strip() for x in form.get("models", [""])[0].split(",") if x.strip()]
                if candidates:
                    MODEL_IDS = list(dict.fromkeys(candidates))[:100]
                page = ("<!doctype html><html><head><meta charset=utf-8><meta name=referrer content=no-referrer>"
                        "<title>Monica local bridge</title></head><body><h1>Updated in memory</h1>"
                        f"<p>Session configured: {bool(COOKIE_HEADER)}. Model count: {len(MODEL_IDS)}. Cookie was not returned.</p>"
                        f"<p>Local DSH API key: <code>{html.escape(API_KEY)}</code></p>"
                        "<p><a href=\"/setup\">Return to setup</a></p></body></html>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(page)
            except (UnicodeDecodeError, ValueError) as exc:
                self._send_json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return
        if parsed.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not found.", "type": "not_found"}})
            return
        if not self._api_authorized():
            self._send_json(401, {"error": {"message": "Invalid local bridge key.", "type": "authentication_error"}})
            return
        if not COOKIE_HEADER:
            self._send_json(401, {"error": {"message": "Configure the Monica session through the local setup page first.", "type": "authentication_error"}})
            return
        try:
            request = json.loads(self._read_body().decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Request JSON must be an object.")
            model = request.get("model")
            if not isinstance(model, str) or model not in MODEL_IDS:
                raise ValueError("Choose a model ID configured on the local setup page.")
            if model not in MODEL_BOT_UIDS:
                raise ValueError("No Monica bot mapping is configured for this model ID.")
            prompt = format_messages(request.get("messages"))
            stream = bool(request.get("stream", False))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return
        request_id = "chatcmpl-" + uuid.uuid4().hex
        try:
            if stream:
                self._stream_reply(request_id, model, prompt)
            else:
                text = asyncio.run(self._collect_reply(model, prompt))
                self._send_json(200, {"id": request_id, "object": "chat.completion", "created": 0, "model": model,
                                      "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # Never return raw upstream bodies or request headers.
            self._send_json(502, {"error": {"message": _upstream_message(exc), "type": "upstream_error"}})

    async def _collect_reply(self, model: str, prompt: str) -> str:
        pieces: list[str] = []
        async for text in _monica_stream_reply_retrying(model, prompt):
            pieces.append(text)
        return "".join(pieces)

    def _stream_reply(self, request_id: str, model: str, prompt: str) -> None:
        loop = asyncio.new_event_loop()
        iterator = _monica_stream_reply_retrying(model, prompt).__aiter__()
        try:
            first = loop.run_until_complete(iterator.__anext__())
        except StopAsyncIteration:
            first = None
        except Exception as exc:
            try:
                loop.run_until_complete(iterator.aclose())
            except Exception:
                pass
            loop.close()
            self._send_json(502, {"error": {"message": _upstream_message(exc), "type": "upstream_error"}})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(payload: object) -> None:
            self.wfile.write(b"data: " + _json_bytes(payload) + b"\n\n")
            self.wfile.flush()

        def emit_chunk(text: str) -> None:
            if text:
                emit({"id": request_id, "object": "chat.completion.chunk", "created": 0, "model": model,
                      "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})

        try:
            if first is not None:
                emit_chunk(first)
            while True:
                try:
                    text = loop.run_until_complete(iterator.__anext__())
                except StopAsyncIteration:
                    break
                emit_chunk(text)
            emit({"id": request_id, "object": "chat.completion.chunk", "created": 0, "model": model,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except BrokenPipeError:
            return
        except Exception as exc:
            # Headers are already committed; signal an incomplete SSE stream rather than a fake success.
            try:
                error = _json_bytes({"error": {"message": _upstream_message(exc), "type": "upstream_error"}})
                self.wfile.write(b"event: error\ndata: " + error + b"\n\n")
                self.wfile.flush()
            except OSError:
                pass
        finally:
            try:
                loop.run_until_complete(iterator.aclose())
            except Exception:
                pass
            loop.close()


def main() -> None:
    if not MODEL_IDS:
        raise SystemExit("Set MONICA_MODELS to at least one model ID before starting.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Monica web-session bridge is experimental and unofficial.")
    print(f"Local setup URL: http://{HOST}:{PORT}/setup")
    print(f"DSH API base URL: http://{HOST}:{PORT}/v1")
    print("The local DSH API key is displayed only on the localhost setup page.")
    print("Session cookies are memory-only; Ctrl+C stops the server and clears them.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        global COOKIE_HEADER
        COOKIE_HEADER = None
        server.server_close()
        print("Stopped; in-memory Monica session cleared.")


if __name__ == "__main__":
    main()
