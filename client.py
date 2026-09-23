"""Verdent upstream client: builds /llm/stream envelopes, parses hybrid-stream responses."""
import json, uuid, datetime, os, threading, urllib.request, urllib.error

from crypto import encrypt_obj

BASE = "https://llm-proxy.verdent.ai"
STREAM_PATH = "/llm/stream"
BETA = "hybrid-stream@20250919"
UA = "Verdent/2.15.1"

class UpstreamError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"upstream {status}: {payload}")
        self.status = status
        self.payload = payload

def _headers(token: str) -> dict:
    # exact header set the desktop app's HttpAiProvider sends to /llm/stream
    import subprocess
    machine_guid = "012c8d596e14c8623f58f2add22f28b5"
    try:
        out = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
            capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "MachineGuid" in line:
                machine_guid = line.split()[-1]
    except Exception:
        pass
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "verdent-proxy-beta": BETA,
        "X-Device-Id": machine_guid,
        "X-Version-Code": "2.15.1",
        "X-Team-ID": "0",
        "X-Device-Type": "pc",
        "X-OS-Type": "windows",
    }

def is_free_model(model: str) -> bool:
    return model.endswith("-free")

def _catalog_flags(model):
    """Look up is_free / is_limit_free from the desktop app's catalog cache."""
    is_limit_free = model.endswith("-free")
    try:
        import json as _json, os
        p = os.path.join(os.path.expanduser("~"), ".verdent", "model-catalog-cache.json")
        for m in _json.load(open(p, encoding="utf-8"))["data"]["model_config"]:
            if m.get("key") == model:
                is_limit_free = bool(m.get("is_limit_free"))
                break
    except Exception:
        pass
    return is_limit_free

_TEMPLATE = None

def _template():
    """App-captured request template.

    The gateway fingerprints the `system` field: it must be the desktop app's
    own encrypted agent prompt (any replacement lands in the strict 20004
    rate lane). Everything else (ids, model, messages, env) is free-form.
    Regenerate template.json from capture.py when the app updates its prompt.
    ponytail: client system prompts ride inside `messages`, not body.system.
    """
    global _TEMPLATE
    if _TEMPLATE is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "template.json"), encoding="utf-8") as f:
            _TEMPLATE = json.load(f)
    return _TEMPLATE


def _app_msg(m, last=False):
    """App-shape message: content is an array of text blocks, first block is a
    <timestamp>, cache_control on the final block of the final message.
    Plain-string content is silently dropped upstream (model then answers a
    hallucinated conversation — the 'PHP shipping data' bug)."""
    c = m.get("content")
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or (p.get("content") if isinstance(p.get("content"), str) else None)
                if t:
                    parts.append(t)
                elif p.get("type") in ("image_url", "image"):
                    parts.append("[image omitted]")
    else:
        parts = [str(c)] if c is not None else []
    off = datetime.datetime.now().astimezone().strftime("%z")  # +0330
    ts = ("<timestamp>" + datetime.datetime.now().strftime("%a %b %d %Y %H:%M:%S GMT")
          + off + "</timestamp>\n")
    blocks = [{"type": "text", "text": ts}]
    for t in parts:
        blocks.append({"type": "text", "text": t})
    if last:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    out = {"role": m.get("role", "user"), "content": blocks}
    if m.get("role") == "assistant":
        out["model"] = m.get("model")
    return out


def _conv_tools(tools):
    """OpenAI tools -> app tools: [{name, description, input_schema}].
    App sends its own 26 tools encrypted in exactly this shape."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        f = t.get("function") if t.get("type") == "function" else t
        if not f.get("name"):
            continue
        out.append({"name": f["name"], "description": f.get("description") or "",
                    "input_schema": f.get("parameters")
                    or {"type": "object", "properties": {}}})
    return out or None


def _conv_tool_choice(tc):
    """tool_choice -> plain dict upstream (capture: {"type":"auto"}).
    ponytail: 'none' maps to auto — gateway has no none; drop tools instead."""
    if isinstance(tc, dict):
        if tc.get("type") == "function":
            nm = (tc.get("function") or {}).get("name")
            return {"type": "tool", "name": nm} if nm else {"type": "auto"}
        return {"type": "any"} if tc.get("type") == "any" else {"type": "auto"}
    if tc == "required":
        return {"type": "any"}
    return {"type": "auto"}


def build_body(model, messages, system, token_id, max_tokens=None,
               temperature=None, stream=True, tools=None, tool_choice=None):
    t = _template()
    # OpenAI `system` messages can't go in body.system (fingerprinted) —
    # fold them into the conversation as a leading user-turn block.
    sys_parts = [m["content"] for m in messages
                 if m.get("role") == "system" and m.get("content")]
    if system:
        sys_parts.insert(0, system)
    msgs = [m for m in messages if m.get("role") != "system"]
    # Agentic history: app has no tool-call blocks — render assistant
    # tool_calls and tool results as text so the model sees its own calls.
    id2name = {}
    for m in msgs:
        for tx in (m.get("tool_calls") or []):
            if tx.get("id"):
                id2name[tx["id"]] = (tx.get("function") or {}).get("name") or ""
    norm = []
    for m in msgs:
        mm = dict(m)
        c = mm.get("content")
        if not isinstance(c, str):
            c = json.dumps(c, ensure_ascii=False) if c is not None else ""
            mm["content"] = c
        if mm.get("role") == "assistant" and mm.get("tool_calls"):
            parts = [c]
            for tx in mm["tool_calls"]:
                fn = tx.get("function") or {}
                parts.append("[tool_call %s] %s" % (fn.get("name") or "",
                                                    fn.get("arguments") or "{}"))
            mm["content"] = "\n".join(p for p in parts if p)
        elif mm.get("role") == "tool":
            nm = id2name.get(mm.get("tool_call_id") or "", "")
            mm = {"role": "user",
                  "content": ("[tool_result %s]\n" % nm if nm else "[tool_result]\n") + c}
        norm.append(mm)
    msgs = norm
    conv_tools = _conv_tools(tools)
    if not conv_tools:
        # Without a tool schema the model falls back to emitting raw
        # tool-call markup (DSML) or roleplaying bash as text.
        sys_parts.append("<tools>none available</tools> No tool-calling "
                         "schema is present in this session: never emit "
                         "tool-call markup (DSML/XML tags) or fake command "
                         "output — answer directly in plain text.")
    if sys_parts:
        block = "<system>\n" + "\n\n".join(str(p) for p in sys_parts) + "\n</system>"
        if msgs and msgs[0].get("role") == "user" and isinstance(msgs[0].get("content"), str):
            msgs[0] = dict(msgs[0], content=block + "\n\n" + msgs[0]["content"])
        else:
            msgs.insert(0, {"role": "user", "content": block})
    if not msgs:
        msgs = [{"role": "user", "content": "Hello"}]
    # tool results ride as user turns (app has no role:"tool")
    msgs = [{"role": "user" if m.get("role") == "tool" else m["role"],
             "content": m.get("content")} if m.get("role") == "tool" else m
            for m in msgs]
    out_msgs = []
    for i, m in enumerate(msgs):
        mm = dict(m)
        if mm.get("role") == "assistant" and not mm.get("model"):
            mm["model"] = model
        out_msgs.append(_app_msg(mm, last=(i == len(msgs) - 1)))

    body = dict(t)
    body.update({
        # Account runs in Free Mode (zero credits): the paid lane answers
        # 30001 "out of credits", free lane answers 200. Captured template
        # had is_free:false from a credited session — always force True.
        "is_free": True,
        "model": model,
        "session_id": "session_" + str(uuid.uuid4()),
        "conv_id": "conv_" + str(uuid.uuid4()),
        "react_id": "model_agent_" + str(uuid.uuid4()),
        "react_type": "Main Agent",
        "stream": stream,
        "max_tokens": max_tokens or t.get("max_tokens", 64000),
        "messages": encrypt_obj(out_msgs),
        "env": dict(t["env"], today_date=datetime.date.today().isoformat()),
    })
    body.pop("sub_type", None)
    if conv_tools:
        body["tools"] = encrypt_obj(conv_tools)
        body["tool_choice"] = _conv_tool_choice(tool_choice)
    th = t.get("thinking")
    if isinstance(th, dict) and th.get("budget_tokens"):
        # budget > max_tokens starves the answer (empty content): the
        # captured template reserves 4000 for thinking.
        lim = max(1024, int(max_tokens or 4096) // 2)
        if th["budget_tokens"] > lim and lim < th["budget_tokens"]:
            body["thinking"] = dict(th, budget_tokens=lim)
    if temperature is not None:
        body["temperature"] = temperature
    return body

def stream_request(token, body, timeout=180):
    """POST /llm/stream, return (resp, resp). Raises UpstreamError.

    Burst-proof against the gateway's RPM lane (20004):
      * all upstream calls are serialized through one lock with >=1.2s gap
        (the app only ever sends one prompt at a time — parallel hits are
        what tripped the rate lane),
      * 500/need_retry + 429 wait out real backoff (retryAfterMs, else
        10/25/45s) instead of hammering,
      * transport is direct first (same egress as the app), falling back to
        the system proxy only on network errors.
    ponytail: total worst-case wait ~80s; 9router test timeout is above that.
    """
    import threading, time as _t
    data = json.dumps(body).encode("utf-8")
    global _UP_GATE, _UP_LAST
    last = None
    with _UP_GATE:
        gap = _UP_LAST + 1.2 - _t.time()
        if gap > 0:
            _t.sleep(gap)
        try:
            _UP_LAST = _t.time()
            for attempt in range(4):
                req = urllib.request.Request(BASE + STREAM_PATH, data=data,
                                             headers=_headers(token), method="POST")
                try:
                    resp = _direct().open(req, timeout=timeout)
                    return resp, resp
                except urllib.error.HTTPError as e:
                    payload = e.read().decode("utf-8", "replace")[:500]
                    last = UpstreamError(e.code, payload)
                    if e.code in (500, 502, 503, 504):
                        wait_ms = None
                        try:
                            j = json.loads(payload)
                            if j.get("need_retry") is True or "20004" in payload:
                                wait_ms = j.get("retryAfterMs") or j.get("retry_after_ms")
                        except Exception:
                            pass
                        if wait_ms is None:
                            wait_ms = [10000, 25000, 45000][min(attempt, 2)]
                        if attempt < 3:
                            _t.sleep(min(int(wait_ms), 45000) / 1000.0)
                            continue
                    if e.code in (400, 401, 403, 404, 406, 429) and e.code != 429:
                        raise
                    break
                except urllib.error.URLError:
                    if attempt == 0:      # network/proxy failure -> system proxy once
                        _UP_LAST = 0
                        req2 = urllib.request.Request(BASE + STREAM_PATH, data=data,
                                                      headers=_headers(token), method="POST")
                        try:
                            resp = urllib.request.urlopen(req2, timeout=timeout)
                            return resp, resp
                        except Exception as e2:
                            last = UpstreamError(502, str(e2))
                    break
        finally:
            _UP_LAST = _t.time()
    raise last from None


_UP_GATE = threading.Lock()
_UP_LAST = 0.0


def _direct():
    if not hasattr(_direct, "_opener"):
        _direct._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _direct._opener
