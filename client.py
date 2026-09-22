"""Verdent upstream client: builds /llm/stream envelopes, parses hybrid-stream responses."""
import json, uuid, urllib.request, urllib.error

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

def build_body(model, messages, system, token_id, max_tokens=4096,
               temperature=None, stream=True):
    sid = str(uuid.uuid4())
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
                "today_date": "2026-09-22"},
        "encrypt": True,
        "is_eco": False,
        "is_auto": False,
        "is_free": is_free_model(model),
        "is_limit_free": False,
        "native_api": False,
    }
    if temperature is not None:
        body["temperature"] = temperature
    return body

def stream_request(token, body, timeout=120):
    """POST /llm/stream, return (headers, byte_iterator). Raises UpstreamError."""
    req = urllib.request.Request(BASE + STREAM_PATH,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers=_headers(token), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp, resp
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")[:500]
        raise UpstreamError(e.code, payload) from None
