# Copilot Digest Remote MCP Connector - Design Document

> **Status:** Draft
> **Author:** Lin, Yin
> **Date:** 2026-04-17
> **Module Name:** `qwenpaw.mcp_server`
> **Type:** New top-level module (ships as console script `qwenpaw-mcp`)
> **Companion to:** [copilot_digest.md](./copilot_digest.md)

---

## 1. Problem Statement

The Copilot Digest skill runs inside a local QwenPaw server and is reached today through QwenPaw's own web console, voice channel, or drop folder. Users who live in **claude.ai (web) or the Claude mobile app** have no way to talk to their own Copilot Digest agent — every reading-list interaction requires context-switching out to QwenPaw.

We want Claude to invoke Copilot Digest directly, from any device tied to the user's Anthropic account: "save this URL", "what's new today?", "tell me about #3", "ingest this PDF". The user's own QwenPaw instance handles the request, preserving their workspace, interests, and knowledge base.

## 2. Solution Overview

Ship `qwenpaw-mcp serve`: a local HTTP MCP server that exposes Copilot Digest over the network. A public tunnel (Cloudflare Tunnel / ngrok / Tailscale Funnel) gives claude.ai a reachable HTTPS URL. Bearer-token auth gates access. Three tools cover the full surface:

- `send_message(text)` — natural-language chat, routed by SKILL.md.
- `ingest_file(path, title?)` — local-path file ingestion (PDF/CSV/TXT/MD).
- `reset_session()` — fresh conversation context.

The MCP server is a thin transport adapter: no in-process agent construction, no duplicated session wiring. It POSTs to `POST /api/agents/{id}/console/chat`, consumes SSE, returns final assistant text.

---

## 3. Architecture

### 3.1 Runtime Topology

```
Claude mobile / claude.ai
       │
       │ HTTPS · streamable-HTTP MCP · Authorization: Bearer qp_mcp_…
       ▼
Public URL  (Cloudflare Tunnel / ngrok / Tailscale Funnel)
       │
       │ HTTPS
       ▼
qwenpaw-mcp serve   on 127.0.0.1:8089
  ├─ BearerAuthMiddleware
  └─ FastMCP (streamable-http)
       │
       │ HTTP on loopback (auth-bypassed by QwenPaw)
       ▼
QwenPaw FastAPI  on 127.0.0.1:8088
  └─ POST /api/agents/{agentId}/console/chat  (SSE stream)
       │
       ▼
QwenPawAgent → Copilot Digest SKILL.md routing
```

The MCP server runs on the **same machine** as QwenPaw. The tunnel makes only the MCP proxy reachable; QwenPaw itself is never exposed. QwenPaw's localhost auth bypass (`src/qwenpaw/app/auth.py`) is the intended path for the MCP→QwenPaw hop — authentication lives one layer up, between Claude and the MCP proxy.

### 3.2 Why HTTP Proxy, Not In-Process

| Option | Chosen? | Notes |
|---|---|---|
| HTTP proxy — MCP server POSTs to `/api/agents/{id}/console/chat` | ✅ | Reuses runner, session mgmt, chat history, MCP-client wiring, tool guard, mission mode. Zero server-side changes. |
| In-process — construct `QwenPawAgent` directly | ❌ | Duplicates `ChatManager`, `TaskTracker`, `ChannelManager`, secret loading, memory manager. High churn. |

### 3.3 Why Streamable-HTTP, Not Stdio

| Transport | Target clients | v1? |
|---|---|---|
| Streamable-HTTP (MCP 2025-06-18 spec) | claude.ai web, Claude mobile, Claude Desktop custom connectors | ✅ |
| Stdio | Claude Code, Claude Desktop's local extensions | ❌ (out of scope — this user doesn't use Claude Code) |

Mobile and web can only consume **remote connectors**: they cannot spawn local subprocesses. That eliminates stdio for the target use case. Stdio can come back as a secondary mode in v2 if Claude Desktop users ask for it; the tool contract won't change.

### 3.4 Tool Surface

| Tool | Signature | Purpose |
|---|---|---|
| `send_message` | `(text: str) -> str` | Forward natural-language text; return assistant reply. |
| `ingest_file` | `(path: str, title: str \| None = None) -> str` | Hand a local PDF/CSV/TXT/MD to Copilot Digest's ingest pipeline by absolute path. |
| `reset_session` | `() -> str` | Mint a fresh `session_id` for the current MCP session. |

A single generic chat tool beats a structured tool-per-intent surface because SKILL.md *already* contains the intent-routing logic. `ingest_file` is the one exception: file attachments are mechanically different from text (path semantics, filesystem parity requirement) and deserve an explicit affordance so Claude doesn't have to guess that "ingest /path/to/thing.pdf" is a valid natural-language instruction.

---

## 4. Key Repo Facts

| Fact | Source |
|---|---|
| Auth bypasses `127.0.0.1`/`::1` → no token needed for MCP→QwenPaw loopback | `src/qwenpaw/app/auth.py` |
| Default QwenPaw port 8088; running port persisted via `write_last_api()` | `src/qwenpaw/cli/app_cmd.py`, `src/qwenpaw/config/utils.py` |
| Right endpoint for request→response is `POST /api/agents/{agentId}/console/chat`, returns SSE | `src/qwenpaw/app/routers/console.py:68`, mounted via `agent_scoped.py:70` under `/api` at `_app.py:575` |
| `/api/v1/agents/.../messages` in `SKILL.md:457` is fire-and-forget — **do not use for MCP** | `src/qwenpaw/app/routers/messages.py:78` |
| Base agent `QwenPawAgent(ToolGuardMixin, ReActAgent)` — we proxy HTTP, don't call `reply()` | `src/qwenpaw/agents/react_agent.py:73` |
| QwenPaw consumes MCP but does not expose any — no `mcp`/`fastmcp` dep today | — |

---

## 5. Module Layout

Top-level `src/qwenpaw/mcp_server/`, shipped as console script `qwenpaw-mcp`.

| Path | Purpose |
|---|---|
| `src/qwenpaw/mcp_server/__init__.py` | Package marker; re-export `main`. |
| `src/qwenpaw/mcp_server/__main__.py` | `python -m qwenpaw.mcp_server` entry. |
| `src/qwenpaw/mcp_server/server.py` | `FastMCP` instance; `send_message`, `ingest_file`, `reset_session`. |
| `src/qwenpaw/mcp_server/client.py` | Async `httpx` client; POSTs `/console/chat`; parses SSE. |
| `src/qwenpaw/mcp_server/auth.py` | Bearer-token load/generate; ASGI middleware enforcing `Authorization: Bearer …`. |
| `src/qwenpaw/mcp_server/voice.py` | Voice-output directive text + mode helper. |
| `src/qwenpaw/mcp_server/cli.py` | Click `serve` subcommand; boots session state; runs uvicorn. |
| `src/qwenpaw/mcp_server/README.md` | Setup (generate token, run tunnel, register in claude.ai, troubleshoot). |
| `pyproject.toml` | Add `mcp>=1.2.0` optional extra `[mcp]`; add `qwenpaw-mcp` console script. |

**Why top-level, not nested under the skill dir** — the wrapper is skill-agnostic; any QwenPaw skill works through the same `send_message` proxy. Copilot Digest is just the first blessed recipe. Top-level placement keeps the entrypoint discoverable and reuses `read_last_api()` for port discovery.

No changes to the FastAPI app, routers, middleware, or skill code.

---

## 6. CLI & Registration

### 6.1 CLI Surface

```
qwenpaw-mcp serve [--host 127.0.0.1] [--port 8089]
                  [--base-url URL] [--agent-id ID]
                  [--token-file PATH] [--print-token]
                  [--timeout SECONDS] [--log-level LEVEL]
                  [--allow-public-bind]
```

**Defaults**

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | Loopback only — tunnel is the sole public ingress. Refuses `0.0.0.0` unless `--allow-public-bind` is also set. |
| `--port` | `8089` | Avoid collision with QwenPaw's 8088. |
| `--base-url` | `read_last_api()` → `http://{host}:{port}`; fallback `http://127.0.0.1:8088` | QwenPaw backend. |
| `--agent-id` | `copilot-digest` | Points at a dedicated agent scoped to the `copilot_digest` skill (`qwenpaw agents create --name 'Copilot Digest' --agent-id copilot-digest --skill copilot_digest`). Avoids exposing the broader `default` agent's skill surface through the bearer token. |
| `--token-file` | `~/.qwenpaw/mcp_token` | Auto-generated on first run; chmod 600. |
| `--print-token` | off | If set, print the token once to stderr on startup. Recommended on first run. |
| `--timeout` | `180` seconds | Copilot Digest ingests can take 30–120s. |
| `--log-level` | `info` | Logs → stderr. HTTP access logs have `Authorization` redacted. |

### 6.2 `pyproject.toml`

```toml
[project.optional-dependencies]
mcp = ["mcp>=1.2.0"]

[project.scripts]
qwenpaw-mcp = "qwenpaw.mcp_server.cli:main"
```

### 6.3 Public URL

**Recommended — Cloudflare Tunnel** (free, durable HTTPS, quick tunnels need no account):

```
cloudflared tunnel --url http://127.0.0.1:8089
```

Prints something like `https://lovely-possum-42.trycloudflare.com`. For a stable domain, create a named tunnel (requires Cloudflare account + domain).

**Alternatives** — ngrok (paid tier for stable URL), Tailscale Funnel (requires tailnet), self-hosted reverse proxy.

### 6.4 Registering in claude.ai

Custom remote connectors are a paid-tier feature (Pro / Team / Enterprise). Steps:

1. Settings → Profile → Connectors → **Add custom connector**.
2. **URL**: tunnel HTTPS URL (append `/mcp` if FastMCP's streamable-http app exposes it there — verify during implementation).
3. **Authentication**: Bearer token → paste `qp_mcp_…` from `~/.qwenpaw/mcp_token`.
4. Enable in a chat, ask *"Save this URL: https://example.com"*.

On **Claude mobile** (same Anthropic account): enable the connector from mobile's Settings → Connectors.

---

## 7. Auth

- **Token generation**: `secrets.token_hex(32)` prefixed `qp_mcp_`, written via `os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)` on first run.
- **Middleware** — `BearerAuthMiddleware` around FastMCP's ASGI app:
  1. Read `Authorization` header; must start with `Bearer `.
  2. Constant-time compare (`hmac.compare_digest`).
  3. On mismatch/missing: HTTP 401 + `WWW-Authenticate: Bearer realm="qwenpaw-mcp"`, no body.
- **Rotation**: delete `~/.qwenpaw/mcp_token`, restart `qwenpaw-mcp serve`, re-paste into claude.ai.
- **MCP→QwenPaw hop** is loopback → auth-bypassed. MCP server forwards no tokens to QwenPaw.

---

## 9. Implementation Sketch

### 9.1 `server.py`

1. `mcp = FastMCP("qwenpaw-copilot-digest")`.
2. `@mcp.tool()` `send_message(text: str) -> str` — delegates to `client.send_message(text)`.
3. `@mcp.tool()` `ingest_file(path: str, title: str | None = None) -> str` — delegates to `client.ingest_file(path, title)`.
4. `@mcp.tool()` `reset_session() -> str` — replaces current session's `session_id` with `uuid4().hex`.
5. `cli.main()` wraps `mcp.streamable_http_app()` in `BearerAuthMiddleware`, runs under uvicorn.

### 9.2 `client.py` — `send_message`

1. POST to `{base_url}/api/agents/{agent_id}/console/chat` with:
   ```python
   {
     "channel": "console",
     "user_id": USER_ID,
     "session_id": SESSION_ID,
     "input": [{"content": [{"type": "text", "text": text}]}],
   }
   ```
3. Consume SSE; extract the last `object == "message"` / `status == "completed"` event's concatenated text.
4. On non-200, timeout, SSE error frame → return a descriptive string (see §11).

> **Event schema caveat.** Exact field paths come from `agentscope_runtime.engine.schemas.agent_schemas` (pinned `agentscope-runtime==1.1.3`). Log the first few SSE frames to stderr during implementation to confirm `event["content"][0]["text"]` vs. `event["message"]["content"]` — don't guess.

### 9.3 `client.py` — `ingest_file`

1. Resolve: `Path(path).expanduser().resolve()`.
2. Validate: exists + is_file + readable → else `"File not found / not readable: {path}"`.
3. Classify: `.pdf` → pdf, `.csv` → csv, `.txt`/`.md` → text. Else `"Unsupported file type '{suffix}'. Copilot Digest handles PDF, CSV, TXT, and MD."`.
4. Build a natural-language prompt routed through SKILL.md §3.1:
   ```
   Save this file to my reading list: {abs_path}
   Source type: {pdf|csv|text}
   [Title: {title}]   ← only if provided
   ```
5. Delegate to `client.send_message(prompt)`.

**Path pass-through, not upload.** Works because v1's MCP server shares a filesystem with QwenPaw. No 10 MB `/console/upload` cap; preserves original location for `ingest.py`'s archive step.

**Mobile caveat** (documented in README): when talking from your phone, `path` refers to a file on the **laptop running QwenPaw**, not the phone. For true phone→agent file ingest, drop files into `{workspace}/inbox/` via iCloud/Dropbox/Syncthing and say "scan my inbox" (SKILL.md §3.3).

### 9.4 Session Handling

- FastMCP session context keys `session_id` per MCP session → two devices don't collide.
- `user_id = "mcp_claude"` — stable, recognizable in QwenPaw logs.
- `reset_session()` replaces the current session's id with fresh `uuid4().hex`.

---

## 10. User-Side Prerequisites

Documented in `README.md`:

1. QwenPaw server running: `copaw app --host 127.0.0.1 --port 8088`.
2. A **dedicated** agent provisioned with `copilot_digest` as its sole skill — a bearer-token holder can trigger every skill on the target agent, so we point the MCP server at a scoped agent rather than `default`:
   ```
   qwenpaw agents create \
     --name "Copilot Digest" \
     --agent-id copilot-digest \
     --skill copilot_digest
   ```
   The MCP server's `--agent-id` defaults to `copilot-digest` to match.
3. `BRIEFER_INTERESTS` env set or user has completed Copilot Digest's interest-config flow.
4. `cloudflared` (or ngrok, Tailscale) installed for the public tunnel.
5. claude.ai Pro / Team / Enterprise subscription (custom connectors are paid-tier).

Not shipped in v1: workspace provisioning / skill enablement automation from the MCP wrapper.

---

## 11. Error Handling

All errors return as **tool string results** — no exceptions cross the MCP boundary.

| Failure | Tool returns |
|---|---|
| QwenPaw server down (ConnectError) | `"Cannot reach QwenPaw at {base_url}. Is 'copaw app' running?"` |
| 404 on `/agents/{id}/console/chat` | `"Agent '{agent_id}' not found. Check --agent-id."` |
| 503 `Channel Console not found` | `"Console channel not registered on agent '{agent_id}'."` |
| Timeout (default 180 s) | `"QwenPaw did not finish within {timeout}s. Long ingests may still be running — check the QwenPaw console."` |
| SSE error frame | `"QwenPaw error: {msg}"` |
| No terminal event | `"(agent produced no text response)"` |
| `ingest_file` path invalid | `"File not found / not readable: {path}"` |
| `ingest_file` unsupported ext | `"Unsupported file type '{suffix}'. Copilot Digest handles PDF, CSV, TXT, and MD."` |

At the MCP transport layer (not tool-level):
- Missing/invalid bearer → HTTP 401, no body.
- Invalid JSON-RPC → MCP spec-standard error.

---

## 12. Dependencies

- `mcp>=1.2.0` as optional extra (`pip install "qwenpaw[mcp]"`).
- `httpx>=0.27.0`, `uvicorn>=0.40.0` already base deps.
- No other new deps.
- On `ImportError` of `mcp`, `cli.main()` raises `"Install with: pip install 'qwenpaw[mcp]'"`.

---

## 13. Verification

1. Start QwenPaw: `copaw app --host 127.0.0.1 --port 8088`.
2. Provision an agent with `copilot_digest` enabled.
3. Install: `pip install -e ".[mcp]"` then `qwenpaw-mcp --help`.
4. Run MCP: `qwenpaw-mcp serve --print-token`. Copy token from stderr.
5. Local sanity: `curl -H "Authorization: Bearer qp_mcp_…" http://127.0.0.1:8089/mcp` — expect 200 or MCP handshake. Without header → 401.
6. Start tunnel: `cloudflared tunnel --url http://127.0.0.1:8089`. Copy HTTPS URL.
7. Register in claude.ai: Settings → Connectors → Add custom → URL + Bearer token.
8. **End-to-end (URL, text mode)**: in a claude.ai chat with the connector enabled, say *"Save this URL to my reading list: https://example.com"*. Verify the article appears under `workspaces/default/skills/copilot_digest/articles/`.
9. **End-to-end (voice, mobile)**: on Claude mobile, enable the connector, tap voice, say *"What's new today?"*. Response should be short, conversational, no markdown artifacts. Follow-up *"Tell me more about the first one"* should resolve correctly (session continuity).
10. **End-to-end (PDF)**: with a PDF at `/Users/me/papers/sample.pdf` on the QwenPaw machine, say *"Ingest /Users/me/papers/sample.pdf"*. Article + summary should appear in the workspace.

---

## 14. Security Call-Outs (ship in README)

- **Bearer token = full agent access.** Anyone with it can read saved articles, trigger ingests, invoke any tools the agent has. Treat like an SSH key.
- **Tunnel URL is semi-secret.** Public URLs get scanned — prefer ephemeral quick-tunnels or IP-allowlisted named tunnels.
- **MCP server binds `127.0.0.1` by default.** The tunnel is the sole public ingress. CLI refuses `0.0.0.0` unless `--allow-public-bind` is also passed — direct internet binding is a foot-gun.
- **QwenPaw's localhost auth bypass** is not a concern here; the MCP→QwenPaw hop is on loopback by design.

---

## 15. Out of Scope for v1

- **Stdio transport** — dropped; add back as secondary mode in v2 if Claude Desktop users ask.
- **OAuth 2.1** — deferred. Bearer token covers personal use. OAuth is the v2 migration path for any wider distribution; tool contract stays stable so connector re-registration is the only user-visible change.
- **Cancellation via `/chat/stop`** — long ingests run to completion. v2.
- **Binary upload via `/console/upload`** for `ingest_file` — path pass-through only. Mobile file drops go through inbox.
- **Per-user tokens** — single bearer shared across devices.
- **Workspace / skill provisioning** from the MCP wrapper.
- **Streaming partial replies** to Claude's TTS mid-ingest.
- **Always-on hosting guidance** beyond a brief README note — user brings their own uptime.

---

## 16. Critical Files

| Path | Role |
|---|---|
| `src/qwenpaw/mcp_server/server.py` | *(new)* FastMCP, tool registration |
| `src/qwenpaw/mcp_server/client.py` | *(new)* SSE httpx client |
| `src/qwenpaw/mcp_server/auth.py` | *(new)* bearer middleware |
| `src/qwenpaw/mcp_server/cli.py` | *(new)* Click CLI, uvicorn bootstrap |
| `src/qwenpaw/mcp_server/voice.py` | *(new)* voice directive |
| `pyproject.toml` | *(modify)* `mcp` extra, `qwenpaw-mcp` console script |
| `src/qwenpaw/app/routers/console.py` | *(reference)* authoritative HTTP contract |
| `src/qwenpaw/config/utils.py` | *(reference)* `read_last_api()` for port discovery |
| `src/qwenpaw/agents/skills/copilot_digest/SKILL.md` | *(reference)* intent routing |
