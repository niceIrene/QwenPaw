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

Hub, audit, providers and token usage have not yet been connected to SQL
repositories. This phase supports one application instance. Session
leases, online backend migration, recovery and activation are outside its scope;
the unused prototype implementations have been removed. The standalone legacy
history import helper remains, but it is not an application migration command.

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
