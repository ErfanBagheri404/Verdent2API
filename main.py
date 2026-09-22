"""Verdent2API - OpenAI-compatible proxy for Verdent (llm-proxy.verdent.ai).

Interactive menu, same shape as WorkBuddy2API / ClineDesktop2API.
"""
import argparse, json, os, sys, time, urllib.request

import auth as A
from version import __version__

BANNER = r"""
+=====================================================+
|              Verdent2API  v{ver:<6}                  |
|   OpenAI-compatible proxy for Verdent (verdent.ai)  |
|   Upstream: llm-proxy.verdent.ai/llm/stream         |
+=====================================================+
""".format(ver=__version__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 61023

def usage_windows():
    """Free/Eco window ratios from the account API (same call the app makes)."""
    tok = A.get_token()
    if not tok:
        return None
    req = urllib.request.Request(
        "https://api.verdent.ai/verdent/usage_windows?team_id=0",
        headers={"Authorization": "Bearer " + tok, "User-Agent": "Verdent/2.15.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("data") or {}
    except Exception:
        return None

def cmd_status():
    a = A.load_auth()
    if not a:
        print("  Not logged in. Choose option 5 first.")
        return
    exp = a.get("expire_at_ms", 0)
    left = max(0, (exp - time.time() * 1000) / 3600000) if exp else float("inf")
    print(f"  Logged in  : {'yes' if A.is_valid(a) else 'EXPIRED'}")
    print(f"  User       : {a.get('user_id') or 'n/a'}")
    print(f"  Expires in : {left:.1f} h")
    print(f"  Store      : {A.AUTH_PATH}")
    w = usage_windows()
    if w:
        for name in ("free_mode", "eco_mode"):
            m = w.get(name) or {}
            used5 = m.get("ratio_5h", 0)
            used7 = m.get("ratio_7d", 0)
            print(f"  {name:<10} : used {used5:.1f}% /5h, {used7:.1f}% /7d, "
                  f"available={m.get('is_available')}")

def cmd_models():
    from server import catalog_models, model_object
    ms = catalog_models()
    if not ms:
        print("  No catalog found. Start the Verdent desktop app once.")
        return
    print(f"  {len(ms)} models:")
    for m in ms:
        tag = " [FREE]" if str(m.get("key", "")).endswith("-free") else ""
        print(f"    - {m.get('key')}{tag}  {m.get('display_name') or m.get('label') or ''}")

def cmd_test_chat():
    token = A.get_token()
    if not token:
        print("  Not logged in.")
        return
    cmd_models()
    model = input("  Model [deepseek-v4.1-flash-free]: ").strip() or "deepseek-v4.1-flash-free"
    msg = input("  Message [Reply with exactly OK]: ").strip() or "Reply with exactly OK"
    from client import build_body, stream_request, UpstreamError
    body = build_body(model, [{"role": "user", "content": msg}],
                      "You are a helpful assistant.", "cli", stream=True)
    t0 = time.time()
    try:
        resp, reader = stream_request(token, body, timeout=120)
    except UpstreamError as e:
        print(f"  Upstream error {e.status}: {e.payload[:300]}")
        return
    buf, out = "", []
    while True:
        chunk = reader.read(4096)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            for line in frame.split("\n"):
                if line.startswith("data:"):
                    p = line[5:].strip()
                    if p == "[DONE]":
                        buf = ""
                        break
                    try:
                        o = json.loads(p)
                    except Exception:
                        continue
                    if o.get("type") == "content_block_delta":
                        d = o.get("delta", {})
                        if d.get("type") == "text_delta":
                            out.append(d.get("text", ""))
                    if o.get("type") in ("stream_error", "error"):
                        print("  error:", str(o)[:200])
        if not chunk:
            break
    print(f"  [{time.time()-t0:.1f}s] {''.join(out)[:600]}")

def cmd_start_server(host, port, api_key, headless=False):
    from server import start_server
    srv = start_server(host, port, api_key=api_key)
    url = f"http://{host}:{port}"
    print(f"\n  Verdent2API listening")
    print(f"  OpenAI base : {url}/v1")
    print(f"  Models      : {url}/v1/models")
    print(f"  Health      : {url}/healthz")
    if api_key:
        print(f"  API key     : (client must send it as Bearer key)")
    print("  Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        srv.server_close()

def main():
    ap = argparse.ArgumentParser(prog="Verdent2API")
    ap.add_argument("--no-menu", action="store_true", help="run server directly")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--api-key", default=None, help="require this key on /v1/*")
    ap.add_argument("--login", action="store_true", help="login then exit")
    args = ap.parse_args()

    if args.login:
        A.login(interactive=True)
        return

    if args.no_menu:
        if not A.get_token():
            print("Not logged in. Run: python main.py --login")
            sys.exit(1)
        cmd_start_server(args.host, args.port, args.api_key, headless=True)
        return

    print(BANNER)
    if not A.get_token():
        print("  No credentials found.\n")
        print("  Press Enter to open the browser and log in...")
        input()
        if not A.login(interactive=True):
            print("  Press Enter...")
            input()
            return
    while True:
        print("  1. Status")
        print("  2. Start server")
        print("  3. List models")
        print("  4. Test chat")
        print("  5. Re-login")
        print("  6. Quit")
        c = input("\n  > ").strip()
        if c == "1": cmd_status()
        elif c == "2": cmd_start_server(args.host, args.port, args.api_key)
        elif c == "3": cmd_models()
        elif c == "4": cmd_test_chat()
        elif c == "5": A.login(interactive=True)
        elif c in ("6", "q", "Q"): break
    print("  Bye.")

if __name__ == "__main__":
    main()
