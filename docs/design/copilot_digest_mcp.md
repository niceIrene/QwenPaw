# Copilot Digest MCP Connector - Design Document

> **Status:** Draft
> **Author:** Lin, Yin
> **Date:** 2026-04-16
> **Module Name:** `qwenpaw.mcp_server`
> **Type:** New top-level module (ships as console script `qwenpaw-mcp`)
> **Companion to:** [copilot_digest.md](./copilot_digest.md)

---

## 1. Problem Statement

The Copilot Digest skill lives inside a running QwenPaw server and is reached today through QwenPaw's own web console, voice channel, or drop-folder. Users who already live in Claude Code / Claude Desktop / the Claude mobile app have no way to talk to their own Copilot Digest agent from Claude's chat UI — every reading-list interaction requires context-switching out to QwenPaw.

We want Claude to be able to invoke Copilot Digest directly: "save this URL", "what's new today?", "tell me about #3", "draft action items from yesterday's briefing" — handled by the user's own QwenPaw instance with its workspace, interests, and knowledge base intact.

## 2. Solution Overview

Ship a thin **MCP (Model Context Protocol) stdio server** — `qwenpaw-mcp` — that Claude clients launch as a subprocess. The server proxies every Claude turn into a single HTTP request to QwenPaw's existing agent endpoint, streams the SSE response, and returns the final assistant text. One generic MCP tool, `send_message(text: str) -> str`, covers the full Copilot Digest surface because SKILL.md already routes by natural language (ingest / briefing / discussion / draft / export).

The wrapper is intentionally minimal: no in-process agent construction, no duplicated session/memory wiring, no per-skill-script tool surface. It is a transport adapter.

---

## 3. Architecture

### 3.1 High-Level Flow

```
Claude Code / Desktop / Mobile
          │
          │ MCP (stdio, JSON-RPC)
          ▼
    qwenpaw-mcp (this module)
          │
          │ HTTP POST (SSE)
          ▼
  QwenPaw FastAPI server
  (already running locally)
          │
          │ channel dispatch
          ▼
   QwenPawAgent.reply(Msg)
          │
          │ loads SKILL.md
          ▼
   Copilot Digest routing
  (ingest / brief / discuss …)
```

### 3.2 Why HTTP Proxy, Not In-Process

| Option | Chosen? | Notes |
|---|---|---|
| **HTTP proxy** — `qwenpaw-mcp` POSTs to `/api/agents/{id}/console/chat` | ✅ | Reuses existing runner, session mgmt, chat history, MCP-client wiring, tool guard, mission mode. Zero server-side changes. |
| **In-process** — construct `QwenPawAgent` directly from `runner.py:500-508` pattern | ❌ | Would duplicate `ChatManager`, `TaskTracker`, `ChannelManager`, secret loading, memory manager, agent lifecycle. High churn, high maintenance. |
| **Both** (flag-switched) | ❌ | Doubles the test matrix for no new capability in v1. |

### 3.3 Transport

**stdio only** for v1. Works with `claude mcp add` (Claude Code) and `claude_desktop_config.json` (Claude Desktop). Simplest auth story — no tokens, no TLS, no public URL.

Remote HTTP/SSE MCP (Claude.ai web/mobile connectors) is explicitly deferred; it requires auth token plumbing that QwenPaw's localhost-only auth bypass does not cover.

### 3.4 Tool Surface

| Tool | Signature | Purpose |
|---|---|---|
| `send_message` | `(text: str) -> str` | Forward natural-language text to Copilot Digest; return its reply. |
| `reset_session` | `() -> str` | Mint a fresh `session_id` to start a new conversation without restarting the MCP process. |

A single generic chat tool beats a structured tool-per-intent surface because SKILL.md *already* contains the intent-routing logic. Duplicating it in MCP tool descriptors would be dead weight and would drift out of sync.

---

## 4. Key Facts About the Existing Code

| Fact | Source |
|---|---|
| Auth bypasses `127.0.0.1`/`::1` → no token needed for localhost MCP | `src/qwenpaw/app/auth.py` |
| Default server port is 8088; running port persisted by `write_last_api()` | `src/qwenpaw/cli/app_cmd.py`, `src/qwenpaw/config/utils.py` |
| The right endpoint for request→response is `POST /api/agents/{agentId}/console/chat`, returns SSE | `src/qwenpaw/app/routers/console.py:68` mounted via `agent_scoped.py:70` under `/api` at `_app.py:575` |
| `/api/v1/agents/.../messages` referenced in `SKILL.md:457` resolves to `/api/messages/send`, which is fire-and-forget → **do not use for MCP** | `src/qwenpaw/app/routers/messages.py:78` |
| Base agent is `QwenPawAgent(ToolGuardMixin, ReActAgent)`; programmatic `reply()` exists but this plan does not call it | `src/qwenpaw/agents/react_agent.py:73,1136` |
| Skill loading is per-workspace via `resolve_effective_skills(workspace_dir, channel_name)` | `src/qwenpaw/agents/skills_manager.py:1166` |
| QwenPaw already *consumes* MCP (`src/qwenpaw/app/mcp/stateful_client.py`) but does not *expose* any | — |

---

## 5. Module Layout

Top-level `src/qwenpaw/mcp_server/`, shipped as console script `qwenpaw-mcp`.

| Path | Purpose |
|---|---|
| `src/qwenpaw/mcp_server/__init__.py` | Package marker; re-export `main`. |
| `src/qwenpaw/mcp_server/__main__.py` | `python -m qwenpaw.mcp_server` entry. |
| `src/qwenpaw/mcp_server/server.py` | `FastMCP` instance; `send_message`, `reset_session` tools. |
| `src/qwenpaw/mcp_server/client.py` | Async `httpx` client; POSTs `/console/chat`; parses SSE; returns final text. |
| `src/qwenpaw/mcp_server/cli.py` | Click CLI; boots session state; runs `mcp.run(transport="stdio")`. |
| `src/qwenpaw/mcp_server/README.md` | Setup / `claude mcp add` / Claude Desktop JSON / troubleshooting. |
| `pyproject.toml` | Add `mcp>=1.2.0` optional extra `[mcp]`; add `qwenpaw-mcp` console script. |

**Why top-level, not nested under the skill dir** — the wrapper is skill-agnostic; any QwenPaw skill works through the same `send_message` proxy. Copilot Digest is just the first blessed recipe. Top-level placement keeps the entrypoint discoverable (`qwenpaw-mcp --help` sits alongside `qwenpaw`/`copaw`) and lets it reuse `read_last_api()` for port discovery.

No changes to the FastAPI app, routers, middleware, or skill code.

---

## 6. CLI & Registration

### 6.1 CLI Surface

```
qwenpaw-mcp [--base-url URL] [--agent-id ID] [--user-id ID]
            [--session-id ID] [--timeout SECONDS] [--log-level LEVEL]
```

**Defaults**

| Flag | Default | Notes |
|---|---|---|
| `--base-url` | `read_last_api()` → `http://{host}:{port}`; fallback `http://127.0.0.1:8088` | Port discovery reuses existing QwenPaw helper. |
| `--agent-id` | `default` | Matches QwenPaw's default single-agent id. |
| `--user-id` | `mcp_claude` | Recognizable sender in QwenPaw logs & chat history. |
| `--session-id` | `uuid4().hex` at process start | Stable for the life of the process → preserves conversational memory across Claude turns. |
| `--timeout` | `180` seconds | Copilot Digest ingests run scripts + LLM calls; 30 s is too short. |
| `--log-level` | `warning` | **All logs → stderr.** stdout is reserved for MCP JSON-RPC frames. |

### 6.2 `pyproject.toml`

```toml
[project.optional-dependencies]
mcp = ["mcp>=1.2.0"]

[project.scripts]
qwenpaw-mcp = "qwenpaw.mcp_server.cli:main"
```

### 6.3 Claude Code

```
claude mcp add copilot-digest \
  --transport stdio \
  -- qwenpaw-mcp --base-url http://127.0.0.1:8088 --agent-id default
```

### 6.4 Claude Desktop `claude_desktop_config.json`

```json
{
  "mcpServers": {
    "copilot-digest": {
      "command": "qwenpaw-mcp",
      "args": [
        "--base-url", "http://127.0.0.1:8088",
        "--agent-id", "default"
      ]
    }
  }
}
```

If Claude Desktop's PATH does not include the QwenPaw venv's `bin/` dir, substitute an absolute command path, e.g. `/Users/me/.venvs/qwenpaw/bin/qwenpaw-mcp`.

---

## 7. Implementation Sketch

### 7.1 `server.py`

1. `mcp = FastMCP("qwenpaw-copilot-digest")`.
2. `@mcp.tool()` `send_message(text: str) -> str` — delegates to `client.send_message(text)`.
3. `@mcp.tool()` `reset_session() -> str` — replaces module-level `session_id` with `uuid4().hex`; returns a confirmation.
4. `cli.main()` parses args, stashes `base_url / agent_id / user_id / session_id / timeout` on a module-level config, then calls `mcp.run(transport="stdio")`.

### 7.2 `client.py` — SSE Consumer

`async def send_message(text: str) -> str:`

1. Build payload:

   ```python
   {
     "channel": "console",
     "user_id": USER_ID,
     "session_id": SESSION_ID,
     "input": [{"content": [{"type": "text", "text": text}]}],
   }
   ```

2. `async with httpx.AsyncClient(timeout=TIMEOUT) as c:` →
   `async with c.stream("POST", f"{BASE_URL}/api/agents/{AGENT_ID}/console/chat", json=payload) as resp:`

3. Non-200 → raise; caller translates to a user-visible error string (see §9).

4. Iterate `resp.aiter_lines()`:
   - Skip blanks.
   - `data: <json>` → `json.loads(line[6:])`.
   - Track the **last** event where `object == "message"` with `status == "completed"`; extract `content[*].text` and concatenate.
   - Accept `object == "response"` with `status == "completed"` as a backup terminal signal.
   - Detect `{"error": "..."}` frames (the router emits these on stream error — see `console.py:137`).

5. Return concatenated assistant text. If no terminal event was seen, return `"(agent produced no text response)"`.

> **Event schema caveat.** Exact field paths come from `agentscope_runtime.engine.schemas.agent_schemas` (pinned to `agentscope-runtime==1.1.3`). During implementation, log the first few SSE frames to stderr and confirm the exact path (`event["content"][0]["text"]` vs. `event["message"]["content"]`) rather than guessing. This is the one empirical step in the build.

### 7.3 Stop Handling

`POST /api/agents/{id}/console/chat/stop?chat_id=...` exists but `chat_id` is minted server-side and not returned in a header — it's embedded in SSE events. For v1, skip explicit stop; rely on server-side timeouts. Documented limitation.

---

## 8. Session Handling

- **One MCP process = one session.**
- `session_id` is generated at startup (`uuid4().hex`) and reused for every `send_message` call.
- Rationale: QwenPaw's console channel keys conversational memory by `session_id` (see `resolve_session_id` in `console.py:93`). A stable id is what makes "What did I save yesterday?" / "Tell me about #3" / "Summarize that article" work across Claude turns.
- `reset_session()` mints a fresh UUID for users who want to start over without restarting Claude.
- `user_id = "mcp_claude"` is stable so the sender is recognizable in logs and chat history.

---

## 9. User-Side Prerequisites

Documented in `README.md`. The MCP server assumes:

1. QwenPaw FastAPI server running locally: `copaw app --host 127.0.0.1 --port 8088` (or the port passed via `--base-url`).
2. An agent provisioned with id matching `--agent-id` (default install ships `default`).
3. That agent's workspace has `copilot_digest` enabled — either `{workspace_dir}/skills/copilot_digest/` exists or it is listed in `{workspace_dir}/skill_manifest.json`.
4. `BRIEFER_INTERESTS` env set **or** the user has completed Copilot Digest's interest-config flow (writes `config.json` in the skill workspace).

**Not shipped in v1:** workspace provisioning / skill enablement automation from the MCP wrapper. The wrapper never writes to `workspaces/`.

---

## 10. Error Handling

All errors surface as **tool string results** — no exceptions cross the MCP boundary, so Claude can reason about them instead of getting an opaque protocol error.

| Failure mode | Wrapper behavior |
|---|---|
| QwenPaw server down (`httpx.ConnectError`) | `"Cannot reach QwenPaw at {base_url}. Is 'copaw app' running?"` |
| 404 on `/agents/{id}/console/chat` | `"Agent '{agent_id}' not found. Check --agent-id."` |
| 503 `Channel Console not found` | `"Console channel not registered on agent '{agent_id}'."` |
| 401 (auth enabled, remote base url) | `"Authentication required. v1 only supports localhost auth-bypass."` |
| httpx timeout (default 180 s) | `"QwenPaw did not finish within {timeout}s. Long ingests may still be running — check the QwenPaw console."` |
| SSE error frame `{"error": ...}` | `"QwenPaw error: {msg}"` |
| No terminal event | `"(agent produced no text response)"` |

---

## 11. Dependencies

- Add `mcp>=1.2.0` as an **optional extra** (`pip install "qwenpaw[mcp]"`) — keeps the base install lean.
- `httpx>=0.27.0` is already a base dep.
- No other new deps.
- On `ImportError` of `mcp`, `cli.main()` raises a clear `"Install with: pip install 'qwenpaw[mcp]'"` hint.

---

## 12. Verification

1. **Start QwenPaw**: `copaw app --host 127.0.0.1 --port 8088`.
2. **Provision** an agent with `copilot_digest` enabled via the QwenPaw web console Skills tab, or confirm `workspaces/default/skills/copilot_digest/` exists.
3. **Install**: `pip install -e ".[mcp]"` then `qwenpaw-mcp --help`.
4. **MCP inspector smoke test**:

   ```
   mcp dev qwenpaw-mcp -- --base-url http://127.0.0.1:8088 --agent-id default
   ```

   Verify `send_message` appears in the tool list; invoke with `{"text": "hello"}`; confirm a text response.
5. **Register in Claude Code**:

   ```
   claude mcp add copilot-digest --transport stdio -- qwenpaw-mcp --base-url http://127.0.0.1:8088 --agent-id default
   claude mcp list
   ```

6. **End-to-end**: in a Claude Code session, ask *"Save this URL to my reading list: https://example.com"*.
   - Claude invokes `copilot-digest.send_message`.
   - QwenPaw logs show the ingest pipeline running.
   - The article appears under `workspaces/default/skills/copilot_digest/articles/`.
   - Follow-up *"What did I save today?"* produces a ranked briefing referencing that article — proves session continuity.

---

## 13. Out of Scope for v1

- HTTP/SSE MCP transport (stdio only).
- Auth token injection for remote QwenPaw instances (requires work in `src/qwenpaw/security/secret_store.py` + TLS).
- Separate MCP tools per skill script (`ingest.py`, `rank_and_summarize.py`, …) — routing stays in SKILL.md.
- Workspace / skill provisioning from the MCP wrapper.
- Best-effort `/chat/stop` on cancellation.
- Claude Desktop connector management UI.

---

## 14. Critical Files (Reference)

| Path | Role |
|---|---|
| `src/qwenpaw/mcp_server/server.py` | *(new)* FastMCP, tool registration |
| `src/qwenpaw/mcp_server/client.py` | *(new)* SSE httpx client |
| `src/qwenpaw/mcp_server/cli.py` | *(new)* Click CLI, session bootstrap |
| `pyproject.toml` | *(modify)* `mcp` extra, `qwenpaw-mcp` console script |
| `src/qwenpaw/app/routers/console.py` | *(reference)* authoritative HTTP contract |
| `src/qwenpaw/config/utils.py` | *(reference)* `read_last_api()` for port discovery |
| `src/qwenpaw/agents/skills/copilot_digest/SKILL.md` | *(reference)* intent routing happens here |
