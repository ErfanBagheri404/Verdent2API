"""Verdent upstream client: builds /llm/stream envelopes, parses hybrid-stream responses."""
import json, uuid, datetime, urllib.request, urllib.error

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
    return {
        "content-type": "application/json",
        "authorization": " ".join(["Bearer", token]),
        "cookie": "".join(["token=", token]),
        "verdent-proxy-beta": BETA,
        "OS": "win32", "CPU-Arch": "x64", "agent_type": "ts_agent",
        "X-Version-Code": "2.15.1",
        "X-Device-ID": "012c8d596e14c8623f58f2add22f28b5",
        "X-Device-Type": "desktop", "X-OS-Type": "windows",
        "User-Agent": UA,
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

def build_body(model, messages, system, token_id, max_tokens=4096,
               temperature=None, stream=True):
    sid = str(uuid.uuid4())
    is_limit_free = _catalog_flags(model)
    body = {
        "channel": "deck",
        "model": model,
        "session_id": "session_" + sid,
        "conv_id": "conv_" + sid,
        "react_id": "model_agent_" + str(uuid.uuid4()),
        "react_type": "Main Agent",
        "sub_type": "",
        "stream": stream,
        "max_tokens": max_tokens,
        "system": encrypt_obj(system or "You are a helpful assistant."),
        "messages": encrypt_obj(messages),
        "agent_name": "VerdentDeck",
        "env": {"platform": "win32", "os_version": "", "shell": "",
                "today_date": datetime.date.today().isoformat()},
        "encrypt": True,
        "is_eco": False,
        "is_auto": False,
        "is_free": is_free_model(model),
        "is_limit_free": is_limit_free,
        "native_api": False,
    }
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
