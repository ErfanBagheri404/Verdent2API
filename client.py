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


def build_body(model, messages, system, token_id, max_tokens=None,
               temperature=None, stream=True):
    t = _template()
    # OpenAI `system` messages can't go in body.system (fingerprinted) —
    # fold them into the conversation as a leading user-turn block.
    msgs = []
    sys_parts = [m["content"] for m in messages
                 if m.get("role") == "system" and m.get("content")]
    if system:
        sys_parts.insert(0, system)
    msgs = [m for m in messages if m.get("role") != "system"]
    if sys_parts:
        block = "<system>\n" + "\n\n".join(str(p) for p in sys_parts) + "\n</system>"
        if msgs and msgs[0].get("role") == "user" and isinstance(msgs[0].get("content"), str):
            msgs[0] = dict(msgs[0], content=block + "\n\n" + msgs[0]["content"])
        else:
            msgs.insert(0, {"role": "user", "content": block})
    if not msgs:
        msgs = [{"role": "user", "content": "Hello"}]

    body = dict(t)
    body.update({
        "model": model,
        "session_id": "session_" + str(uuid.uuid4()),
        "conv_id": "conv_" + str(uuid.uuid4()),
        "react_id": "model_agent_" + str(uuid.uuid4()),
        "react_type": "Main Agent",
        "stream": stream,
        "max_tokens": max_tokens or t.get("max_tokens", 64000),
        "messages": encrypt_obj(msgs),
        "env": dict(t["env"], today_date=datetime.date.today().isoformat()),
    })
    body.pop("tools", None)
    body.pop("tool_choice", None)
    body.pop("sub_type", None)
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
