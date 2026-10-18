# SQL storage integration

SQLite remains the default backend. Existing installations retain their local
SQLite history and JSON session files unless they select PostgreSQL explicitly.

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

Chat lists, titles, groups, pinned and archived status use the same PG runtime.
The complete workspace registry is one revisioned JSON record; stale updates
fail rather than overwrite newer metadata. Reused workspace managers rebind to
the newly opened runtime. SQLite still uses the existing `chats.json` file.

Cron definitions and execution history now use the same workspace PG runtime.
The registry and history are separate revisioned records. History retains the
existing per-job limit (normally 50); concurrent callbacks in one repository
serialize their history updates, while stale writes from another instance fail.
Cron shutdown cancels and drains callbacks before session storage is closed,
including cancellation history and checkpoint cleanup. SQLite continues using
`jobs.json` and `jobs_history`. Existing local jobs are not automatically imported.

Hub, audit, inbox events and providers remain on their existing local stores.
This phase supports one application instance. Session leases, online backend
migration, recovery and activation are outside its scope; the unused prototype
implementations have been removed. A manifest-based offline import command
covers legacy sessions, history and chat metadata; it does not implement
online migration or backend activation.

Writes validate ownership without updating or locking the global metadata row.
History sequence allocation serializes only writers to the same agent store;
retries may leave sequence gaps while retaining the original event identity.
Session revision checks still reject stale saves.

Existing prototype databases keep their metadata columns and any old
`leases`/`migration_jobs` tables; initialization never deletes them. New databases
do not create those two tables. Inactive datasets are still rejected. Stop all
workers before administrative database changes; running old migration code
alongside this runtime is unsupported. This remains an integration under
development, not a complete application storage switch.

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
different deployment is rejected. Session pools are owned per workspace, and token usage owns one additional
application-wide pool. Size connection limits for both.

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

Changing the backend does not migrate existing data. Use a separate development
deployment for PG testing; legacy JSON sessions are not automatically read by
the PG session store.

## Verification

The storage suite runs against SQLite and, when
`QWENPAW_TEST_POSTGRES_DSN` is set, a real PostgreSQL server. It creates isolated
test schemas and removes them after testing:

```sh
pytest tests/unit/storage tests/unit/app/chats tests/unit/app/workspace \
  tests/unit/agents/hooks/test_session_hook.py
```

## Token usage

Selecting PostgreSQL also selects database-backed token usage. Producers keep
synchronous, in-memory recording; the application flushes batches every ten
seconds, on reads and on graceful shutdown. Each batch has a stable ID retained
across retry or cancellation. The batch marker and daily aggregates commit in
one transaction, so retrying a committed write after a lost response does not
count the same usage twice. Existing tables are reused; no schema upgrade is
required. Queries read only daily records within the requested date range.
Provider/model/agent dimensions and prompt-cache statistics remain unchanged.

Usage storage is initialized before the application serves requests. It has its
own application-wide pool, which stays open until workspace shutdown finishes
and the final usage flush runs. Connection failures propagate; there is no
fallback to local JSON when PG is selected. SQLite keeps its original JSON
buffer. Startup events queued before backend selection are preserved.

As with the existing file buffer, unflushed data lives in process memory. A hard
crash or failed final flush can lose those events; this is not a durable event
queue. Batch markers currently have no automatic retention policy. Existing
local usage JSON is not imported by `import-workspace`, which only handles
workspace sessions, history and chat metadata.

## Offline import into PostgreSQL

Stop **all** application workers before importing. Initialize the PG namespace
with `storage init`, but do not start the application: importing requires a
workspace scope that has never been opened. It rejects existing stores or chat
registries, even if empty. The command does not change the selected backend.

Create an explicit JSON manifest. Paths are relative to the manifest file.
For the current workspace runtime, set both `workspace_id` and `agent_id` to
the workspace agent ID:

```json
{
  "workspace_id": "default",
  "agent_id": "default",
  "history": "workspace/history.db",
  "chats": "workspace/chats.json",
  "sessions": [
    {
      "session_id": "example-session",
      "user_id": "example-user",
      "channel": "console",
      "path": "workspace/sessions/console/example-user_example-session.json"
    }
  ]
}
```

Use actual paths and original identities from your installation; the sample
filename is illustrative. Include every session you want to retain, including
orphaned session files absent from the chat list. The command imports only
listed checkpoints, not a directory scan. Every chat and every history session
must have a mapping. `user_id` and `channel` must match runtime request keys
exactly; they are required even when empty. Filenames are never decoded to
infer those values. A session mapping may omit `path` for history-only or
metadata-only sessions. Omit `history` when there is no legacy history database.

```sh
qwenpaw storage --config /path/to/pg-config.json import-workspace \
  /path/to/import.json --offline
qwenpaw storage --config /path/to/pg-config.json check
```

History is copied through a temporary SQLite backup, preserving sequence
numbers and the allocation high-water mark. Raw legacy session IDs are mapped
to the PG channel/user/session keys. Ambiguous legacy IDs shared by multiple
users or channels are rejected when importing history. Chat metadata,
checkpoints and history commit in one transaction; any failure rolls back the
whole import. Source files are never rewritten. After correcting a failed
manifest, rerun the command. A successful import cannot be rerun over the same
scope. Keep the original files as the rollback source and verify the imported
workspace before resuming application traffic.

## Remaining integration boundaries

Selecting PG is still a partial application storage switch:

- Hub data and governance audit still use their own SQLite databases.
- Inbox events use local JSON; provider/model selections use provider files.
- Backups, workspace export/clone/import and doctor checks still contain
  filesystem assumptions. A file backup is not a backup of PG records.
- `import-workspace` covers sessions, history and chats, not cron definitions,
  cron execution history or application-wide usage totals.
- Memory services have separate persistence configuration; selecting this
  backend does not select PostgreSQL for their memory/vector stores.

Workspace files, attachments, driver YAML, secrets and Shadow Git checkpoints
also remain local. Moving database records does not replace their filesystem
requirements. Do not treat this configuration as a stateless or multi-replica
deployment mode.
