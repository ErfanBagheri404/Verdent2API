# Verdent2API

OpenAI-compatible API proxy for **Verdent** (verdent.ai) free models.

Turns your Verdent account into a standard OpenAI endpoint — works with any
client that speaks `/v1/chat/completions` (9router, LobeChat, Open WebUI, SDKs...).

- **26 models** from your Verdent catalog, free tier flagged (`glm-5.3-flash-free`, `deepseek-v4.1-flash-free`)
- **OpenAI-compatible**: `/v1/chat/completions` (streaming + non-stream), `/v1/models`, `/healthz`
- **Real auth**: PKCE browser login against `login.verdent.ai`, same flow as the desktop app
- **No desktop app required** — tokens are stored locally and refreshed automatically
- Streaming responses translated from Verdent's `hybrid-stream` SSE to OpenAI chunks
- `tool_calls` and `reasoning_content` preserved

## How it passes the gateway

Verdent's gateway fingerprints the encrypted `system` field: it must carry the
desktop app's own agent prompt (captured once into `template.json`). Any
custom system prompt lands in a strict `20004` rate lane, so OpenAI-style
`system` messages are folded into the first user message instead. If Verdent
ships a new app version and requests start 429ing, regenerate:

```sh
python capture_app_request.py 61024          # in a second shell
# set ~/.verdent/config.json  internal.llmProxy = http://127.0.0.1:61024
# (or relaunch app with VERDENT_LLM_PROXY_BASE_URL=http://127.0.0.1:61024)
# send one prompt in the app, then restore the config (capture_app_request
# prints instructions; body is captured to capture.log — copy its `system`,
# `thinking`, `effort`, `max_tokens`, `temperature`, `model_catalog_version`
# into template.json and restore config.json from its .bak)
```

> Open source, for maintenance/education. You are responsible for complying
> with Verdent's Terms of Service.

## Quick start

```bash
python main.py            # menu: login / start server / models / test chat
# or headless:
python main.py --login
python main.py --no-menu --port 61023
```

First run opens your browser → log in → tokens land in `~/.verdent2api/auth.json`.

Then point any OpenAI client at:

```
base_url = http://localhost:61023/v1
api_key  = anything (or set a real key with --api-key)
```

```bash
curl http://localhost:61023/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"hi"}]}'
```

## Menu

```
1. Status        account + token expiry
2. Start server  localhost:61023
3. List models   live catalog from ~/.verdent/model-catalog-cache.json
4. Test chat     pick any model, send a message, see the streamed reply
5. Re-login      new PKCE session
6. Quit
```

## CLI flags

| flag | default | meaning |
|---|---|---|
| `--no-menu` | off | run the server directly (requires login) |
| `--host` | `localhost` | bind address |
| `--port` | `61023` | bind port |
| `--api-key KEY` | off | require `Authorization: Bearer *** on `/v1/*` |
| `--login` | off | run browser login, then exit |

## Endpoints

| method | path | notes |
|---|---|---|
| GET | `/healthz` | `{"ok":true,"has_token":true}` — never needs auth |
| GET | `/v1/models` | OpenAI list, `is_free` flag included |
| GET | `/v1/models/{id}` | one model or 404 |
| POST | `/v1/chat/completions` | `stream:true` (OpenAI SSE) or `stream:false` (assembled JSON) |

## How it works

```
client (OpenAI) ──> Verdent2API :61023 ──> llm-proxy.verdent.ai/llm/stream
                                              │
                                              ├─ Authorization: Bearer ***
                                              ├─ verdent-proxy-beta: hybrid-stream@...
                                              ├─ desktop identity headers
                                              └─ AES-256-GCM encrypted {system, messages}
Verdent hybrid-stream SSE  ──>  translated into OpenAI chat chunks
```

The request envelope (endpoint, encryption key, header set, event schema) was
reverse-engineered from the Verdent desktop app package; the response stream is
re-encoded into standard OpenAI format by `server.py`.

**Free vs paid:** model IDs ending in `-free` set `is_free: true` in the
envelope — Verdent bills nothing for those. Paid models need account credits
(`error 30001` otherwise). One Free-plan rate window applies to free models
(`error 20004` when exceeded).

## Project layout

| file | role |
|---|---|
| `main.py` | menu + CLI entry |
| `auth.py` | PKCE login, token store, refresh |
| `crypto.py` | AES-256-GCM envelope |
| `client.py` | upstream request builder |
| `server.py` | OpenAI-compatible HTTP server + SSE translation |
| `tests/selfcheck.py` | 19 asserts, no network |

## Build the exe

CI builds on every push to `main`: bumps `VERSION`, tags `vX.Y.Z`,
publishes a GitHub Release with Windows/Linux/macOS binaries.

```bash
python -m PyInstaller --onefile --name Verdent2API main.py
```

## License

MIT
