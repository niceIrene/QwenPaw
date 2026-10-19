# SQL storage integration

SQLite remains the default backend. Existing installations retain their local
SQLite history and JSON session files unless they select PostgreSQL explicitly.

## First contribution: review guide

This branch is a deliberately limited follow-up to PR #7954, based on its
original head. It enables PG **sessions and scroll history in a development
deployment**, not a complete application storage migration. The original
migration service, leases, metadata schema and their tests remain intact.

Review the commits in this order:

1. Settings and session repository: explicit identities, revision checks and
   atomic checkpoint-plus-history repository writes.
2. Runtime selection: default local behavior, explicit `storage init/check`,
   session pool ownership and failure propagation from hooks.
3. Scroll integration: await history before checkpoint pointers, close native
   async handles, and construct consistent disposable recall views.
4. File-only operation guards: reject incomplete ZIP transfers, inherited
   session forks and Shadow Git conversation checkpoints before mutation.
5. Invalid session payload handling: malformed or unserializable state raises
   a storage failure instead of being ignored by the session hooks.
6. Shutdown ordering: drain cron callbacks before releasing session storage,
   even though cron definitions themselves remain local in this phase.

Chat/cron/usage SQL repositories, offline application imports, SQL backup/restore
commands and removal of prototype orchestration belong in separate follow-ups.
This split intentionally does not ask the original author to accept those
architectural choices together with runtime integration.

## Current runtime coverage

PostgreSQL currently stores session snapshots through `DatabaseSession`. The
workspace owns its connection pool and closes it during shutdown. Session loads
remember a request-local revision; a concurrent stale save fails instead of
overwriting newer state. Database errors propagate from session hooks instead of
being treated as a missing session or a successful save.

Selecting PostgreSQL now also routes scroll history through the workspace SQL
runtime. History commits before sequence bookkeeping and normal session
checkpoints. Recall reads a fresh, consistent PG snapshot through a temporary
SQLite query database, preserving MemorySpace SQL and FTS5 behavior. Snapshots
retain source sequence numbers, are scoped to the workspace's agent store, and
are removed when the request's context manager closes. A refresh failure raises
instead of returning stale data. The REPL receives only a read-only sandbox mount
for this history snapshot; its existing sandbox requirement remains in force.

Structured `expand` and `recall_tool` queries export only matching rows, up to
MemorySpace's row cap plus one row to retain the truncation notice. Session and
agent filters are applied in SQL before that limit. These small query views do
not build an FTS index. Existing pagination, tool media and saved-output handling
continue to use the same MemorySpace code. Invalid expand ranges, blocked repeat
pages and calendar calculations do not read PostgreSQL.

Search and the arbitrary-SQL REPL still copy the complete agent store in bounded
pages, preserving existing FTS5 ranking and cross-session SQL behavior. Their
transfer and indexing cost remains proportional to history size. A REPL or
search call always refreshes its complete view, even after a bounded lookup;
there is no stale-cache fallback. REPL scratch tables remain in the workspace's
local `.scroll` directory; they are not yet stored in PG. Legacy JSON-to-SQLite
history import is skipped on PG startup; switching backends does not implicitly
import old data.

Chat metadata, cron definitions/history, Hub, audit, providers and token usage
have not yet been connected to SQL repositories. Leases and migration primitives
exist, but application
migration/activation and multi-instance coordination are not connected. This is
an integration under development, not a complete application storage switch.

This first contribution intentionally preserves the original lease and migration
framework. It does not wire those prototypes into live requests or remove them.
Use one application instance and a separate development namespace; online
migration, shared-worker coordination and existing-installation rollout are
outside this phase.

File-only operations cannot transfer PG sessions. Workspace ZIP upload/download,
workspace ZIP backup/restore and changes to PG storage through ZIP restore are
rejected before mutation. Configuration/secrets/skills-only backups remain
available within their existing constraints. Forking a subagent with inherited
session state and Shadow Git conversation checkpoints are also unavailable in
PG mode; automatic Shadow Git snapshots are skipped. Normal PG session saves
are independent of Shadow Git and remain enabled. Back up the database and
matching workspace files while all workers are stopped.

Agent configuration/skill/job copying keeps its existing local-file semantics
in this phase: chats and cron have not moved to PG. Do not use these file copies
as backups of session state or history.

## Configure a development deployment

Merge the following into the root `config.json`:

```json
{
  "storage": {
    "backend": "postgresql",
    "deployment_id": "my-development-deployment",
    "postgresql": {
      "dsn_env": "QWENPAW_POSTGRES_DSN",
      "schema": "qwenpaw",
      "pool_min_size": 1,
      "pool_max_size": 10
    }
  }
}
```

Set `QWENPAW_POSTGRES_DSN` in the application environment to the PostgreSQL
connection URI. Credentials are resolved when connecting, not written into
`config.json`. Keep `deployment_id` stable: an existing namespace belonging to a
different deployment is rejected. Pools are currently owned per workspace, so
size the connection limit for the number of loaded workspaces.

Initialize the empty SQL namespace explicitly before starting the application:

```sh
qwenpaw storage init
qwenpaw storage check
```

Use `qwenpaw storage --config /path/to/config.json check` to inspect a separate
configuration. Relative SQLite paths are resolved from that file's parent.
`check` does not create missing databases or tables. `init` creates an empty
namespace or validates an existing owned store; it does not overwrite an
existing dataset, import legacy sessions, or change configuration. Application
startup only validates initialized storage and does not fall back to local
sessions when PostgreSQL is unavailable.

With `backend: sqlite`, these CLI commands address the **new SQL schema** at
`state/state.db`; existing runtime SQLite history and JSON sessions remain in
their original locations. Neither command migrates or activates that schema for
the legacy runtime.

Changing the backend does not migrate existing data. Until application migration
and activation are connected, use a separate development deployment for PG
testing; legacy JSON sessions are not automatically read by the PG session store.

## Verification

The storage suite runs against SQLite and, when
`QWENPAW_TEST_POSTGRES_DSN` is set, a real PostgreSQL server. It creates isolated
test schemas and removes them after testing:

```sh
pytest tests/unit/storage tests/unit/app/chats tests/unit/app/workspace \
  tests/unit/agents/hooks/test_session_hook.py
```

The regression suites also cover file guards, checkpoint behavior, cron shutdown
and the default SQLite runtime. They do not establish external-model, browser,
Windows/Linux or multi-instance compatibility. Full-history search/REPL cost and
per-workspace pool sizing still need workload-specific evaluation before rollout.

Review validation (macOS, Python 3.11, PostgreSQL 15): 1536 related tests passed,
2 skipped. This included the original migration/lease tests, session/history,
runtime/scroll, workspace lifecycle, backup/checkpoint and affected route suites.
Pre-commit passed across all changed Python files. Tests use isolated PostgreSQL
schemas; no existing deployment data is migrated by this verification.
