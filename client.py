"""Verdent upstream client: builds /llm/stream envelopes, parses hybrid-stream responses."""
import json, uuid, datetime, os, urllib.request, urllib.error

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

def stream_request(token, body, timeout=120):
    """POST /llm/stream, return (resp, resp). Raises UpstreamError.

    Retries 500+need_retry (upstream's own "try again" flag, app behaves
    the same) up to 3 times, honoring retryAfterMs capped at 30s.
    ponytail: total retry budget ~60s; raise cap if free-window flaps longer.
    """
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(3):
        req = urllib.request.Request(BASE + STREAM_PATH, data=data,
                                     headers=_headers(token), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp, resp
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")[:500]
            last = UpstreamError(e.code, payload)
            retry_after_ms = None
            if e.code in (500, 502, 503, 504):
                try:
                    j = json.loads(payload)
                    if j.get("need_retry") is True:
                        retry_after_ms = j.get("retryAfterMs") or j.get("retry_after_ms")
                except Exception:
                    pass
            if retry_after_ms is None and e.code in (500, 502, 503, 504):
                retry_after_ms = 1500 * (attempt + 1)
            if retry_after_ms is None:
                break  # 4xx: not retryable
            wait = min(retry_after_ms, 30000) / 1000.0
            if attempt < 2:
                import time as _t
                _t.sleep(wait)
    raise last from None
