"""OpenAI-compatible HTTP server for Verdent2API."""
import json, os, re, time, uuid, urllib.request
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

# When the tool schema is dropped upstream (capacity stripping, model
# lapsing), the model imitates our history format and writes tool calls as
# plain text: "[tool_call name] {json}" or DSML invoke markup. The heal
# parser below converts both back into structured tool_calls.
_MARKERS = ("[tool_call", "<｜｜DSML｜｜")
_TC_SPAN = re.compile(r"\[tool_call ([A-Za-z0-9_./-]+)\]\s*(\{)")
_DSML_RE = re.compile(r'<｜｜DSML｜｜ invoke name="([^"]+)">(.*?)</｜｜DSML｜｜ invoke>', re.S)
_DSML_PARAM = re.compile(r'<｜｜DSML｜｜ parameter name="([^"]+)"[^>]*>(.*?)</｜｜DSML｜｜ parameter>', re.S)

_TS_RE = re.compile(r"<timestamp>[^<]*</timestamp>\s*")

def _strip_ts(text):
    """Model sometimes echoes Hermes's <timestamp> context marker as its own
    output (bare date lines). It belongs in prompts, not responses."""
    return _TS_RE.sub("", text)

def marker_at_or_after(s, pos):
    """First marker start (full, or partial at end) at/after pos, else None."""
    for m in _MARKERS:
        i = s.find(m, pos)
        if i != -1:
            return i
        for k in range(1, len(m)):
            if s.endswith(m[:k]) and len(s) - k >= pos:
                return len(s) - k
    return None

def _tolerant_args(tool, args):
    """Strict JSON first; on failure repair the common model slip — raw code
    pasted with real newlines/quotes inside a pseudo-JSON arg block.
    Hermes executes code args, so the value only needs to survive as a
    string, not as valid JSON on the wire."""
    try:
        json.loads(args)
        return args
    except Exception:
        pass
    # key scan: "param" : value up to the next key or block end
    out = {}
    keys = [(mm.start(1), mm.group(1)) for mm in
            re.finditer(r'"([A-Za-z_][A-Za-z0-9_]*)"\\s*:', args)]
    if not keys:
        return None
    for idx, (pos, key) in enumerate(keys):
        vstart = args.index(":", pos) + 1
        vend = keys[idx + 1][0] if idx + 1 < len(keys) else len(args)
        val = args[vstart:vend].strip().rstrip(",").strip()
        # strip one outer quote pair, keep the interior verbatim
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        elif val.startswith('"""') or val.endswith('"""'):
            val = val.strip('"')
        # unescape the \\n lits the model uses for newlines inside strings
        val = val.replace("\\\\n", "\n").replace("\\\\t", "\t").replace("\\\\\\\\", "\\\\")
        out[key] = val
    if not out:
        return None
    return json.dumps(out, ensure_ascii=False)

def _parse_text_tool_calls(text):
    """Return (calls, cleaned_text) with markup spans removed."""
    calls, spans = [], []
    for m in _TC_SPAN.finditer(text):
        start = m.end(2) - 1
        # Brace-depth scanning is wrong here: the args string legitimately
        # contains unbalanced Kotlin/JS braces. Region = this call to the next
        # marker, then cut at each '}' and keep the first parse that works.
        region_end = text.find("[tool_call", m.end(2))
        if region_end == -1:
            region_end = len(text)
        region = text[start:region_end]
        cands = [k + 1 for k, ch in enumerate(region) if ch == "}"]
        arg_str, end = None, None
        for e in cands:
            try:
                json.loads(region[:e])
                arg_str, end = region[:e], start + e
                break
            except Exception:
                pass
        if arg_str is None:
            for e in reversed(cands):
                p = _tolerant_args(m.group(1), region[:e])
                if p is not None:
                    arg_str, end = p, start + e
                    break
        if arg_str is None:
            continue
        calls.append({"id": "call_" + uuid.uuid4().hex[:16], "type": "function",
                      "function": {"name": m.group(1), "arguments": arg_str}})
        spans.append((m.start(), end))
    for m in _DSML_RE.finditer(text):
        params = {p.group(1): p.group(2)
                  for p in _DSML_PARAM.finditer(m.group(2))}
        if not params:
            continue
        calls.append({"id": "call_" + uuid.uuid4().hex[:16], "type": "function",
                      "function": {"name": m.group(1),
                                   "arguments": json.dumps(params, ensure_ascii=False)}})
        spans.append(m.span())
    if not calls:
        return [], text
    spans.sort()
    out, last = [], 0
    for a, b in spans:
        out.append(text[last:a])
        last = b
    out.append(text[last:])
    return calls, "".join(out)

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
                          temperature=req.get("temperature"), stream=True,
                          tools=req.get("tools"),
                          tool_choice=req.get("tool_choice"))
        try:
            resp, reader = stream_request(token, body)
        except UpstreamError as e:
            st = 429 if "rate limit" in str(e.payload).lower() else 502
            s, b = oai_err(st, "upstream_error", f"{e.payload[:300]}")
            return self._send(s, b)
        ct = resp.headers.get("content-type", "")
        print(f"[req] model={model} tools={len(req.get('tools') or [])} "
              f"stream={bool(req.get('stream'))} sse={'event-stream' in ct}", flush=True)
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
        # Live text goes out until a tool-markup marker appears; from there
        # we hold back so the healed parse can emit real tool_calls instead
        # of raw markup. client sent tools -> parse eligible.
        client_tools = bool(req.get("tools"))
        emitted_chars = 0
        suppress = False

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
                        blk = obj.get("content_block") or obj.get("content") or {}
                        if blk.get("type") == "tool_use":
                            tool_acc[obj.get("index", blk.get("index", 0))] = {
                                "id": blk.get("id") or "call_" + uuid.uuid4().hex[:12],
                                "name": blk.get("name") or "", "args": ""}
                    elif t == "content_block_delta":
                        d = obj.get("delta") or {}
                        dt = d.get("type")
                        if dt == "text_delta":
                            piece = d.get("text", "")
                            if not suppress:
                                cand = "".join(text) + piece
                                hit = (marker_at_or_after(cand, emitted_chars)
                                       if client_tools else None)
                                if hit is None and emitted_chars == 0:
                                    # response starting with a <timestamp> echo
                                    for k in range(1, 12):
                                        if cand.startswith("<timestamp>"[:k]):
                                            hit = 0
                                            break
                                if hit is not None:
                                    suppress = True
                                    emitted_chars = len("".join(text))
                            text.append(piece)
                            if want_stream and not suppress:
                                emit(chunk({"content": piece}))
                                emitted_chars += len(piece)
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
                        # Real usage arrives only here (message_start
                        # carries a placeholder input_tokens:0).
                        u = obj.get("usage") or {}
                        usage_in = u.get("input_tokens") or usage_in
                        usage_out = u.get("output_tokens") or usage_out
                        done = True
            # final assembled chunk (always emit terminal)
            final_text = _strip_ts("".join(text))
            calls = []
            if client_tools and not tool_acc:
                # Tool schema was dropped upstream and the model wrote the
                # call as text markup — heal it back into structured calls.
                calls, final_text = _parse_text_tool_calls(final_text)
            delta = {}
            if tool_acc:
                delta["tool_calls"] = [
                    {"id": tool_acc[i]["id"], "type": "function",
                     "function": {"name": tool_acc[i]["name"],
                                  "arguments": tool_acc[i]["args"]}}
                    for i in sorted(tool_acc)]
                finish = "tool_calls"
            elif calls:
                delta["tool_calls"] = calls
                finish = "tool_calls"
            if want_stream:
                if reasoning:
                    emit(chunk({"reasoning_content": "".join(reasoning)}))
                if suppress and len(final_text) > emitted_chars:
                    # held-back narration (markup spans already stripped)
                    emit(chunk({"content": final_text[emitted_chars:]}))
                emit(chunk(delta, finish))
                # usage chunk: empty choices + usage (OpenAI include_usage)
                uc = {"id": cid, "object": "chat.completion.chunk",
                      "created": created, "model": model, "choices": [],
                      "usage": {"prompt_tokens": usage_in,
                                "completion_tokens": usage_out,
                                "total_tokens": usage_in + usage_out}}
                emit(uc)
                # [DONE] must be a proper chunk, not raw bytes
                done_pay = b"data: [DONE]\n\n"
                self.wfile.write(("%x\r\n" % len(done_pay)).encode() + done_pay + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                out = {
                    "id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0,
                                 "finish_reason": finish,
                                 "message": {"role": "assistant",
                                             "content": final_text or None,
                                             **({"reasoning_content": "".join(reasoning)}
                                                if reasoning else {}),
                                             **({"tool_calls":
                                                 (calls if not tool_acc else [
                                                 {"id": tool_acc[i]["id"], "type": "function",
                                                  "function": {"name": tool_acc[i]["name"],
                                                               "arguments": tool_acc[i]["args"]}}
                                                 for i in sorted(tool_acc)])} if (tool_acc or calls) else {})}}],
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
        calls = []
        text = _strip_ts(text)
        if req.get("tools"):
            calls, text = _parse_text_tool_calls(text)
        u = d.get("usage") or {}
        out = {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else (d.get("stop_reason") or "stop"),
                         "message": {"role": "assistant", "content": text or None,
                                     **({"tool_calls": calls} if calls else {})}}],
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
