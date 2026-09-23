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
# account has zero credits: free lane forced True (30001 otherwise)
t("body is_free forced to free lane", b["is_free"] is True)
t("body session/conv prefix", b["session_id"].startswith("session_")
  and b["conv_id"].startswith("conv_"))
inner = decrypt_blob(b["messages"])
t("messages are app block arrays", isinstance(inner[0]["content"], list)
  and inner[0]["content"][0]["type"] == "text"
  and "<timestamp>" in inner[0]["content"][0]["text"])
t("user text preserved in block",
  any((p.get("text") or "").endswith("hi") for p in inner[0]["content"]))
t("last block carries cache_control",
  inner[-1]["content"][-1].get("cache_control") == {"type": "ephemeral"})
t("assistant messages carry model", all(
    m.get("model") for m in inner if m.get("role") == "assistant") if any(
    m.get("role") == "assistant" for m in inner) else True)
bsys = build_body("glm-5.3-flash-free",
                  [{"role": "system", "content": "be terse"},
                   {"role": "user", "content": "hi"}], "extra", "id2")
inner2 = decrypt_blob(bsys["messages"])
first_txt = " ".join(p.get("text", "") for p in inner2[0]["content"])
t("client system folded into first user msg",
  inner2[0]["role"] == "user" and "be terse" in first_txt
  and "extra" in first_txt and "<system>" in first_txt)
h = _headers("tok")
t("headers carry beta marker", h["verdent-proxy-beta"].startswith("hybrid-stream"))
t("headers carry X-Team-ID", h["X-Team-ID"] == "0")
from crypto import encrypt_obj, decrypt_blob
bt = build_body("glm-5.3-flash-free",
                [{"role": "user", "content": "ls"}],
                "", "id3",
                tools=[{"type": "function",
                        "function": {"name": "bash",
                                     "description": "run cmd",
                                     "parameters": {"type": "object",
                                                    "properties": {"cmd": {"type": "string"}}}}}],
                tool_choice="auto")
t("tools passthrough encrypted", decrypt_blob(bt["tools"])[0]["name"] == "bash"
  and "input_schema" in decrypt_blob(bt["tools"])[0]
  and bt["tool_choice"] == {"type": "auto"})
bt2 = build_body("glm-5.3-flash-free",
                 [{"role": "assistant", "content": None,
                   "tool_calls": [{"id": "call_1", "type": "function",
                                   "function": {"name": "bash",
                                                "arguments": "{\"cmd\":\"ls\"}"}}]},
                  {"role": "tool", "tool_call_id": "call_1", "content": "x"}],
                 "", "id4")
in4 = decrypt_blob(bt2["messages"])
asst = [m for m in in4 if m["role"] == "assistant"][0]
res = [m for m in in4 if m["role"] == "user"][-1]
t("assistant tool_calls rendered as text",
  "[tool_call bash]" in " ".join(p.get("text", "") for p in asst["content"]))
t("tool results ride as user turns with name",
  "[tool_result bash]" in
  " ".join(p.get("text", "") for p in res["content"]))
t("no tools upstream without tools", "tools" not in bt2)

import server as S
S._server_api_key = None
import urllib.request
print("\nAll checks passed.")
