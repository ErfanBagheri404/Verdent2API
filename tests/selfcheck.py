"""Verdent2API self-check. Run: python tests/selfcheck.py"""
import base64, json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto import encrypt_obj, decrypt_blob, KEY
from server import split_system, model_object, Handler

def t(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    assert cond, name

t("key is 32 bytes", len(KEY) == 32)

blob = encrypt_obj({"a": 1, "b": "hello"})
back = decrypt_blob(blob)
t("encrypt/decrypt roundtrip", back == {"a": 1, "b": "hello"})
t("ciphertext differs from plaintext", blob != json.dumps({"a": 1, "b": "hello"}))

sys_msg, rest = split_system([
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "yo"},
    {"role": "user", "content": "again"},
])
t("system split - system extracted", sys_msg == "be brief")
t("system split - rest length", len(rest) == 3)

m = model_object({"key": "deepseek-v4.1-flash-free", "provider": "deepseek",
                  "display_name": "DS", "description": "d",
                  "context_window_tokens": 128000,
                  "default_max_output_tokens": 32000,
                  "supportsImages": False, "supports_thinking": True})
t("model object id", m["id"] == "deepseek-v4.1-flash-free")
t("model object free flag", m["is_free"] is True)
t("model object owned_by", m["owned_by"] == "deepseek")

ev, data = Handler._parse_frame('event: message_start\ndata: {"type":"message_start"}')
t("parse frame - event", ev == "message_start")
t("parse frame - data", json.loads(data)["type"] == "message_start")
ev2, data2 = Handler._parse_frame("data: [DONE]")
t("parse frame - done", data2 == "[DONE]")

from client import build_body, is_free_model, _headers
t("is_free suffix", is_free_model("glm-5.3-flash-free") is True)
t("is_free paid", is_free_model("glm-5.3-flash") is False)
b = build_body("deepseek-v4.1-flash-free",
               [{"role": "user", "content": "hi"}], None, "id1")
t("body has encrypt flag", b["encrypt"] is True)
t("body uses fingerprinted app system blob", len(b["system"]) > 20000)
t("body is_free mirrors app (false)", b["is_free"] is False)
t("body session/conv prefix", b["session_id"].startswith("session_")
  and b["conv_id"].startswith("conv_"))
t("body messages decryptable", decrypt_blob(b["messages"])[0]["content"] == "hi")
bsys = build_body("glm-5.3-flash-free",
                  [{"role": "system", "content": "be terse"},
                   {"role": "user", "content": "hi"}], "extra", "id2")
inner = decrypt_blob(bsys["messages"])
t("client system folded into first user msg",
  inner[0]["role"] == "user" and "be terse" in inner[0]["content"]
  and "extra" in inner[0]["content"] and "<system>" in inner[0]["content"])
h = _headers("tok")
t("headers carry beta marker", h["verdent-proxy-beta"].startswith("hybrid-stream"))
t("headers carry X-Team-ID", h["X-Team-ID"] == "0")

import server as S
S._server_api_key = None
import urllib.request
print("\nAll checks passed.")
