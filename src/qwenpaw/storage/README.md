# SQL storage integration

SQLite remains the default backend. Existing installations retain their local
SQLite history and JSON session files unless they select PostgreSQL explicitly.

## Current runtime coverage

PostgreSQL currently stores session snapshots through `DatabaseSession`. The
workspace owns its connection pool and closes it during shutdown. Session loads
remember a request-local revision; a concurrent stale save fails instead of
overwriting newer state. Database errors propagate from session hooks instead of
being treated as a missing session or a successful save.

The async history, lease and migration repositories are available, but scroll,
MemorySpace/REPL, chat metadata, Hub, audit, providers and token usage have not yet
been connected to them. In particular, selecting PostgreSQL does **not** move
scroll history out of SQLite or make a multi-instance deployment safe. This is
an integration under development, not a complete application storage switch.

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
