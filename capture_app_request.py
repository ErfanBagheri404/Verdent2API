"""Capture the Verdent app's real /llm/stream request by acting as its LLM proxy.

Start:  python capture.py 61024
App is pointed here via ~/.verdent/config.json  internal.llmProxy
(or env VERDENT_LLM_PROXY_BASE_URL). Every request is appended to capture.log
as one JSON line (headers + raw body), then forwarded verbatim upstream.
"""
import json, sys, traceback, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://llm-proxy.verdent.ai"
LOG = r"C:\Users\mrenm\AppData\Local\hermes\cache\scratch\capture.log"
SKIP_REQ = ("host", "content-length", "connection")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _forward(self, method):
        try:
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n) if n else b""
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"method": method, "path": self.path,
                                    "headers": dict(self.headers),
                                    "body_raw": body.decode("utf-8", "replace")}) + "\n")
            sys.stderr.write(f"[capture] {method} {self.path} bytes={len(body)}\n")
            sys.stderr.flush()

            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_REQ}
            req = urllib.request.Request(UPSTREAM + self.path,
                                         data=body if method in ("POST", "PUT") else None,
                                         headers=hdrs, method=method)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                r = opener.open(req, timeout=600)
                data, code, rh = r.read(), r.status, r.headers
            except urllib.error.HTTPError as e:
                data, code, rh = e.read(), e.code, e.headers

            self.send_response(code)
            for k, v in rh.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            traceback.print_exc()
            try:
                payload = b'{"error":{"message":"capture proxy failure"}}'
                self.send_response(502)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                pass

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_HEAD(self):
        self._forward("HEAD")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "*")
        self.send_header("content-length", "0")
        self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 61024
    print(f"capture proxy on http://127.0.0.1:{port} -> {UPSTREAM}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
