"""OpenAI-compatible HTTP server for Verdent2API."""
import json, os, time, uuid, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from auth import get_token, load_auth, login
from client import build_body, stream_request, UpstreamError, is_free_model

CATALOG = os.path.join(os.path.expanduser("~"), ".verdent", "model-catalog-cache.json")

_server_api_key = None

def catalog_models():
    try:
        d = json.load(open(CATALOG, encoding="utf-8"))
        return [m for m in d["data"]["model_config"] if m.get("key")]
    except Exception:
        return []

def model_object(m):
    """m: upstream catalog entry -> OpenAI model object."""
    return {
        "id": m["key"],
        "object": "model",
        "created": 0,
        "owned_by": m.get("provider") or "verdent",
        "display_name": m.get("display_name") or m.get("label"),
        "description": m.get("description"),
        "context_window": m.get("context_window_tokens"),
        "max_output_tokens": m.get("default_max_output_tokens"),
        "supports_images": m.get("supportsImages"),
        "supports_reasoning": m.get("supports_thinking"),
        "is_free": is_free_model(m["key"]),
    }

def split_system(messages):
    system, rest = "", []
    for m in messages:
        if m.get("role") == "system" and not system:
            system += m.get("content") or ""
        else:
            rest.append(m)
    return system, rest

def oai_err(status, code, msg):
    return status, json.dumps({"error": {"message": msg, "type": code}}).encode()

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def _auth_ok(self):
        if not _server_api_key:
            return True
        got = self.headers.get("authorization", "")
        return got.replace("Bearer ", "", 1).strip() == _server_api_key

    def _send(self, status, body: bytes, ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            a = load_auth()
            self._send(200, json.dumps({"ok": bool(a), "has_token": bool(a)}).encode())
        elif path == "/v1/models":
            if not self._auth_ok():
                s, b = oai_err(401, "invalid_api_key", "bad api key")
                return self._send(s, b)
            objs = [model_object(m) for m in catalog_models()]
            self._send(200, json.dumps({"object": "list", "data": objs}).encode())
        elif path.startswith("/v1/models/"):
            mid = path.rsplit("/", 1)[-1]
            for m in catalog_models():
                if m["key"] == mid:
                    return self._send(200, json.dumps(model_object(m)).encode())
            s, b = oai_err(404, "not_found", f"model {mid} not found")
            self._send(s, b)
        else:
            s, b = oai_err(404, "not_found", "unknown path")
            self._send(s, b)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/v1/chat/completions":
            s, b = oai_err(404, "not_found", "unknown path")
            return self._send(s, b)
        if not self._auth_ok():
            s, b = oai_err(401, "invalid_api_key", "bad api key")
            return self._send(s, b)
        token = get_token()
        if not token:
            s, b = oai_err(401, "authentication_error", "not logged in; run login")
            return self._send(s, b)
        n = int(self.headers.get("content-length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            s, b = oai_err(400, "invalid_request_error", "bad json")
            return self._send(s, b)
        model = req.get("model") or "deepseek-v4.1-flash-free"
        messages = req.get("messages") or []
        if not messages:
            s, b = oai_err(400, "invalid_request_error", "messages required")
            return self._send(s, b)
        system, rest = split_system(messages)
        body = build_body(model, rest, system, str(uuid.uuid4()),
                          max_tokens=int(req.get("max_tokens") or 4096),
                          temperature=req.get("temperature"), stream=True)
        try:
            resp, reader = stream_request(token, body)
        except UpstreamError as e:
            st = 429 if "rate limit" in str(e.payload).lower() else 502
            s, b = oai_err(st, "upstream_error", f"{e.payload[:300]}")
            return self._send(s, b)
        ct = resp.headers.get("content-type", "")
        if "event-stream" in ct:
            self._relay_sse(reader, req, model, want_stream=bool(req.get("stream")))
        else:
            raw = resp.read().decode("utf-8", "replace")
            self._finish_json(raw, req, model, want_stream=bool(req.get("stream")))

    # ---- SSE relay / re-encode ----
    def _relay_sse(self, reader, req, model, want_stream):
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        if want_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
        buf = ""
        text, reasoning, tool_acc = [], [], {}
        finish = "stop"
        usage_in = usage_out = 0
        done = False

        def emit(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")

        def chunk(content, finish_reason=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0,
                                 "delta": content,
                                 "finish_reason": finish_reason}]}

        try:
            while not done:
                data = reader.read(4096)
                if not data:
                    break
                buf += data.decode("utf-8", "replace")
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    ev, payload = self._parse_frame(frame)
                    if payload is None:
                        continue
                    if payload == "[DONE]":
                        done = True
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    t = obj.get("type") or ev or ""
                    if t in ("heartbeat", "ping"):
                        continue
                    if t in ("stream_error", "error"):
                        msg = (obj.get("error") or {}).get("message") or payload[:200]
                        err = chunk({"content": f"\n[upstream error] {msg}"}, "stop")
                        if want_stream:
                            emit(err)
                        else:
                            text.append(f"\n[upstream error] {msg}")
                        done = True
                        break
                    if t == "message_start":
                        usage_in = self._usage_in(obj) or usage_in
                    elif t == "content_block_start":
                        blk = obj.get("content") or {}
                        if blk.get("type") == "tool_use":
                            tool_acc[blk.get("index", 0)] = {
                                "id": blk.get("id") or "call_" + uuid.uuid4().hex[:12],
                                "name": blk.get("name") or "", "args": ""}
                    elif t == "content_block_delta":
                        d = obj.get("delta") or {}
                        dt = d.get("type")
                        if dt == "text_delta":
                            piece = d.get("text", "")
                            text.append(piece)
                            if want_stream:
                                emit(chunk({"content": piece}))
                        elif dt == "thinking_delta":
                            reasoning.append(d.get("thinking", ""))
                        elif dt in ("input_json_delta", "arguments_delta"):
                            idx = obj.get("index", 0)
                            tool_acc.setdefault(idx, {"id": "call_" + uuid.uuid4().hex[:12],
                                                      "name": "", "args": ""})
                            tool_acc[idx]["args"] += d.get("partial_json", "") or \
                                d.get("arguments", "") or ""
                    elif t == "message_delta":
                        u = obj.get("usage") or {}
                        usage_out = u.get("output_tokens") or usage_out
                        sr = (obj.get("delta") or {}).get("stop_reason")
                        if sr == "tool_use":
                            finish = "tool_calls"
                    elif t == "message_stop":
                        done = True
            # final assembled chunk (always emit terminal)
            delta = {}
            if tool_acc:
                delta["tool_calls"] = [
                    {"id": tool_acc[i]["id"], "type": "function",
                     "function": {"name": tool_acc[i]["name"],
                                  "arguments": tool_acc[i]["args"]}}
                    for i in sorted(tool_acc)]
                finish = "tool_calls"
            if want_stream:
                if reasoning:
                    emit(chunk({"reasoning_content": "".join(reasoning)}))
                emit(chunk(delta, finish))
                emit(chunk({}, None) if False else b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                out = {
                    "id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0,
                                 "finish_reason": finish,
                                 "message": {"role": "assistant",
                                             "content": "".join(text) or None,
                                             **({"reasoning_content": "".join(reasoning)}
                                                if reasoning else {}),
                                             **({"tool_calls": [
                                                 {"id": tool_acc[i]["id"], "type": "function",
                                                  "function": {"name": tool_acc[i]["name"],
                                                               "arguments": tool_acc[i]["args"]}}
                                                 for i in sorted(tool_acc)]} if tool_acc else {})}}],
                    "usage": {"prompt_tokens": usage_in, "completion_tokens": usage_out,
                              "total_tokens": usage_in + usage_out},
                }
                self._send(200, json.dumps(out, ensure_ascii=False).encode())
        except Exception:
            try:
                if want_stream:
                    self.wfile.write(b"0\r\n\r\n")
            except Exception:
                pass

    @staticmethod
    def _parse_frame(frame):
        ev, data = None, None
        for line in frame.split("\n"):
            line = line.strip("\r")
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = (data or "") + (line[5:].lstrip() if not data else line[5:])
        return ev, data

    @staticmethod
    def _usage_in(obj):
        try:
            return int(obj["message"]["usage"]["input_tokens"])
        except Exception:
            return 0

    def _finish_json(self, raw, req, model, want_stream):
        """Non-SSE upstream body: Anthropic-style JSON or error."""
        try:
            d = json.loads(raw)
        except Exception:
            s, b = oai_err(502, "upstream_error", raw[:300])
            return self._send(s, b)
        if "error" in d and isinstance(d["error"], dict):
            s, b = oai_err(502, "upstream_error", str(d["error"])[:300])
            return self._send(s, b)
        text = "".join(b.get("text", "") for b in d.get("content", [])
                       if b.get("type") == "text")
        u = d.get("usage") or {}
        out = {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "finish_reason": d.get("stop_reason") or "stop",
                         "message": {"role": "assistant", "content": text or None}}],
            "usage": {"prompt_tokens": u.get("input_tokens", 0),
                      "completion_tokens": u.get("output_tokens", 0),
                      "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)},
        }
        self._send(200, json.dumps(out, ensure_ascii=False).encode())


def start_server(host, port, api_key=None):
    global _server_api_key
    _server_api_key = api_key
    srv = ThreadingHTTPServer((host, port), Handler)
    return srv
