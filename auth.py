"""Auth for Verdent2API: PKCE login + token storage + refresh-aware token retrieval.

Flow (reverse-engineered from Verdent desktop app.asar):
  1. Build authorize URL: {www}/auth?challenge=<S256>&state=<rand>&intent=signin
      &callback=http://127.0.0.1:<port>/auth/callback&ots=deck&source=deck&id=<device>
  2. User logs in; browser redirects to callback with ?code=...
  3. POST {login}/passport/pkce/callback {"code":..., "codeVerifier":...}
      -> {"data": {"token": "<bearer>", "expireTime": <epoch-seconds>}}
  4. Store token; send as `Authorization: Bearer <token>` to llm-proxy.verdent.ai.
"""
import base64, hashlib, http.server, json, os, secrets, threading, time
import urllib.request
from urllib.parse import urlparse, parse_qs, quote

WWW = "https://www.verdent.ai"
LOGIN = "https://login.verdent.ai"
PROXY = "https://llm-proxy.verdent.ai"
UA = "Verdent/2.15.1"

DATA_DIR = os.path.join(os.path.expanduser("~"), ".verdent2api")
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")

def _b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def load_auth():
    try:
        with open(AUTH_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("token") else None
    except Exception:
        return None

def save_auth(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, AUTH_PATH)

def delete_auth():
    try: os.remove(AUTH_PATH)
    except OSError: pass

def is_valid(auth):
    if not auth or not auth.get("token"): return False
    exp = auth.get("expire_at_ms", 0)
    return exp == 0 or time.time() * 1000 < exp - 60000

def refresh(auth):
    """Refresh access token. Refresh tokens rotate - persist whatever comes back."""
    rt = auth.get("refresh_token")
    if not rt:
        return None
    body = json.dumps({"refreshToken": rt}).encode()
    req = urllib.request.Request(LOGIN + "/passport/token/refresh", data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
        data = resp.get("data") or {}
        at = data.get("accessToken") or data.get("access_token")
        if not at:
            return None
        auth["token"] = at
        auth["expire_at_ms"] = int(data.get("accessTokenExpiresAt")
                                   or data.get("expireTime") or 0) * 1000
        new_rt = data.get("refreshToken") or data.get("refresh_token")
        if new_rt:
            auth["refresh_token"] = new_rt
        save_auth(auth)
        return auth
    except Exception:
        return None

def get_token():
    auth = load_auth()
    if is_valid(auth):
        return auth["token"]
    if auth and auth.get("refresh_token"):
        refreshed = refresh(auth)
        if refreshed and is_valid(refreshed):
            return refreshed["token"]
    return None

def login(interactive=True, timeout=300):
    """Run the PKCE flow. Returns token dict or None."""
    code_verifier = _b64u(secrets.token_bytes(32))
    challenge = _b64u(hashlib.sha256(code_verifier.encode()).digest())
    state = _b64u(secrets.token_bytes(32))
    device_id = secrets.token_hex(16)
    box = {}

    class CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            box.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Verdent login captured. Close this tab.</h2>")
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), CB)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    callback = f"http://127.0.0.1:{srv.server_address[1]}/auth/callback"
    url = (f"{WWW}/auth?challenge={challenge}&state={state}&intent=signin"
           f"&callback={quote(callback, safe='')}&ots=deck&source=deck&id={device_id}")

    print(f"\n  Open this URL to log in with Verdent:\n\n  {url}\n")
    if interactive:
        try: os.startfile(url)
        except Exception: pass
    print("  Waiting for login (Ctrl+C to cancel)...")
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and "code" not in box:
            time.sleep(1)
    except KeyboardInterrupt:
        return None
    if "code" not in box:
        print("  Login timed out.")
        return None
    srv.shutdown()

    body = json.dumps({"code": box["code"], "codeVerifier": code_verifier}).encode()
    req = urllib.request.Request(LOGIN + "/passport/pkce/callback", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
        tok = resp["data"]["token"]
        expire_ms = int(resp["data"].get("expireTime", 0)) * 1000
        auth = {"token": tok, "expire_at_ms": expire_ms,
                "user_id": str(resp["data"].get("userId", "") or ""),
                "obtained_at_ms": int(time.time() * 1000)}
        save_auth(auth)
        print("  Login OK.")
        return auth
    except Exception as e:
        body_txt = ""
        if hasattr(e, "read"):
            try: body_txt = e.read()[:300].decode("utf-8", "replace")
            except Exception: pass
        print(f"  Token exchange failed: {e} {body_txt}")
        return None
