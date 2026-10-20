# SQL storage integration

SQLite remains the default backend. Existing installations retain their local
SQLite history and JSON session files unless they select PostgreSQL explicitly.

## Review scope

This follow-up to PR #7954 connects the selected backend to sessions, scroll
history, chat metadata, cron definitions/execution history and token usage.
SQLite retains its existing JSON/SQLite runtime format by default. PostgreSQL
uses SQL repositories for these core services. The original MigrationService,
leases, metadata schema, write fences and regression tests remain intact.

Review the commits in groups: session/history runtime and operation guards;
chat/cron/usage repositories and lifecycle; explicit offline imports; complete
SQL backup/restore; platform validation and this operating procedure. No live
backend switch or automatic restart activation is introduced.

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

Chat metadata, cron definitions/history and usage now use the selected backend.
Cron callbacks drain before their dependencies close; session and history pools
are closed during shutdown. Stale revisions raise conflicts and database errors
propagate without silently falling back to local files. Buffered usage is durable
only after a successful flush; abrupt process termination can lose pending usage.

Use one application process for each deployment namespace. It can host multiple
agents and sessions; this restriction concerns application workers, not agent
count. Existing lease primitives are not enforced throughout live operations,
so multiple replicas/workers are not supported. The preserved metadata write
fence serializes writes and is not a throughput improvement claim.

Hub, audit, providers, local memory, REPL scratch tables and workspace assets
remain file-backed and must be included in a matching filesystem backup.

File-only operations cannot transfer PG sessions. Workspace ZIP upload/download,
workspace ZIP backup/restore and changes to PG storage through ZIP restore are
rejected before mutation. Configuration/secrets/skills-only backups remain
available within their existing constraints. Forking a subagent with inherited
session state and Shadow Git conversation checkpoints are also unavailable in
PG mode; automatic Shadow Git snapshots are skipped. Normal PG session saves
are independent of Shadow Git and remain enabled. Back up the database and
matching workspace files while all workers are stopped.

Copying jobs between agents is rejected in PG mode because file copying cannot
preserve SQL cron state. Configuration and skill copying retains local-file
semantics and is not a database backup.

## Configure PostgreSQL

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

## Hub-managed local and Docker runtimes

The standalone default `QWENPAW_POSTGRES_DSN` is a protected environment name
in Hub. Do not relax Hub's credential filtering or rely on exporting a DSN in
the Hub host shell: managed local runtimes do not inherit arbitrary host
variables, and both provisioners filter tenant credentials.

For each stopped runtime:

1. Choose a unique PostgreSQL schema and deployment ID for that runtime. Do not
   point multiple Hub runtimes at the same namespace: multi-process writers are
   not supported. Use database credentials restricted to the intended namespace.
2. Set `storage.postgresql.dsn_env` to `POSTGRES_DSN` in that runtime's own
   `config.json` (the configuration under its working directory, not the Hub
   control-plane configuration). Keep credentials out of the JSON file.
3. Store `POSTGRES_DSN` in the Hub credential vault using scope
   `runtime:<runtime_id>`. The authenticated `PUT /api/hub/credentials` endpoint
   accepts `{ "scope": "runtime:<runtime_id>", "name": "POSTGRES_DSN",
   "value": "<connection URI>" }`. Runtime-specific values override tenant
   defaults; prefer runtime scope to avoid sharing a database credential.
4. Initialize/check the namespace explicitly before starting the runtime:
   `qwenpaw storage --config /path/to/runtime/config.json init`, followed by
   `check`, using the same application revision and an operator environment
   containing `POSTGRES_DSN`. For existing data follow the offline import
   procedure below before activating the PG settings. These commands do not
   resolve Hub vault secrets themselves.
5. Start the runtime through Hub. The provisioner receives the resolved vault
   credential. A Docker DSN must be reachable from the managed container;
   `localhost` inside the container refers to that container, not the Hub host.
   Initialize from a network context that can reach the same database namespace.
   macOS local runtimes use Seatbelt and deny arbitrary host-loopback ports;
   a DSN pointing at `127.0.0.1` is therefore not supported by that policy.
   Use an endpoint reachable under the existing sandbox policy. Do not weaken
   isolation merely to make a host-local database reachable.
6. Verify runtime readiness and persisted data, then stop/restart and check again.
   Hub's own registry, vault, and other control-plane data remain local and must
   be included in the paired filesystem backup.

## Offline import and manual cutover

Changing the backend does not migrate data. Do not edit the active configuration
until every required import and verification succeeds.

1. Stop all application processes, schedulers and workers. Keep the source
   stopped for the entire operation. Save the original configuration and take a
   filesystem backup of the working directory **and external workspace paths**,
   including sessions, history, chats, jobs, job history, usage, assets, local
   memory, provider settings and secrets. Record paths and application revision.
2. Prepare a separate `candidate.json` with the PG settings above and a stable
   deployment ID. Use an empty, isolated PostgreSQL schema. Set the DSN environment
   variable in the operator shell. Initialize using the candidate configuration:

   ```sh
   qwenpaw storage --config candidate.json init
   ```

3. Prepare one workspace manifest per agent. Paths are relative to the manifest;
   use actual source paths. Both `workspace_id` and `agent_id` must equal the
   runtime agent ID. Enumerate every session explicitly; the importer does not
   discover files or guess identities from names. For example:

   ```json
   {
     "workspace_id": "agent-id",
     "agent_id": "agent-id",
     "history": "source/history.db",
     "chats": "source/chats.json",
     "sessions": [
       {
         "session_id": "session-id",
         "user_id": "user-id",
         "channel": "console",
         "path": "source/sessions/session.json"
       }
     ]
   }
   ```

   `history` may be omitted if no history exists; omit a session's `path` only
   when there is no saved checkpoint. Chat references and history mappings are
   validated; ambiguous identities are rejected. Compare the manifest against
   the complete source inventory, including sessions absent from the chat list.

   ```sh
   qwenpaw storage --config candidate.json import-workspace workspace.json --offline
   ```

4. Import cron for each agent with a separate manifest:

   ```json
   {
     "workspace_id": "agent-id",
     "agent_id": "agent-id",
     "jobs": "source/jobs.json",
     "history": {"job-id": "source/job-history/job-id.json"}
   }
   ```

   Enumerate all existing execution-history files. Import application-wide usage
   once, using the actual legacy usage JSON path:

   ```sh
   qwenpaw storage --config candidate.json import-cron cron.json --offline
   qwenpaw storage --config candidate.json import-usage source/usage.json --offline
   qwenpaw storage --config candidate.json check
   ```

5. Review the `history_references` report from `import-workspace`. Checkpoints
   with scroll references require a history source. The report checks explicit
   sequence endpoints within the mapped session and identifies missing rows
   and persisted IDs without sequence mappings. It does not assume sequence
   ranges are contiguous, nor validate every embedded textual recall reference.
   `requires_review: true` means data was imported but reference completeness
   is unresolved: do not activate until each gap is explained by verified source
   retention or corrected by importing the complete source into a fresh scope.
   The importer cannot infer whether a missing row was intentionally deleted.
   Without an explicit report collector, the import API rejects unresolved
   references instead of silently accepting them.

   Compare import results and target counts with the source inventory. `check`
   checks connectivity, ownership, schema status and row counts; it does **not**
   prove the manifests include every source file or validate all business data.
   Perform the backup/restore rehearsal below before enabling the candidate.
6. Only after successful imports and verification, manually merge the candidate
   storage settings into the active configuration, preserving other application
   settings. Start one application process. Confirm old conversations/history,
   chat lists, cron definitions/history and usage are visible before resuming
   normal traffic. Do not start the old source instance alongside it.

Each workspace or cron import is transactional; the complete deployment import
is **not one transaction**. If a later command fails, previous successful imports
remain in the candidate. Keep the original configuration active and the source
files intact. Correct the failed import or restart with a new empty namespace;
do not blindly rerun successful imports or clear an occupied namespace.

Before new writes reach PG, rollback means restoring the original configuration
and paired files. After PG accepts writes, returning to old SQLite files would
lose those new writes: stop, back up PG and reconcile deliberately. Automatic
reverse migration into the legacy runtime format is not implemented. The original
MigrationService remains available as a storage primitive, but its activation
is not connected to application configuration or live requests.

## Paired backup and restore rehearsal

Stop every application process before taking **both** snapshots. `--offline`
is an operator assertion, not a mechanism that stops or locks out workers.

```sh
qwenpaw storage --config candidate.json backup storage.jsonl --offline
```

Pair this SQL archive with a filesystem snapshot from the same maintenance
window. The archive includes all eight schema tables: metadata, stores, history,
checkpoints, records, usage, leases and migration jobs. It preserves identities,
history high-water marks, checkpoint generations and revisions. Live leases
block backup/restore; stopped workers' leases must expire first. The JSONL format
is version 2; the earlier six-table prototype format is intentionally rejected.
The archive contains sensitive application data and is not encrypted.
Archive publication uses a hard link to avoid overwriting an existing destination.
Use a filesystem supporting hard links (such as APFS or NTFS); FAT/exFAT targets
are not supported by this publication method.

Prepare `rehearsal.json` pointing to another empty schema, with the **same
deployment ID**. Keep all
workers stopped; do not connect an application to the rehearsal namespace.

```sh
qwenpaw storage --config rehearsal.json init
qwenpaw storage --config rehearsal.json restore storage.jsonl --offline
qwenpaw storage --config rehearsal.json check
```

Restore validates the archive digest, counts, identity and schema before committing
its transaction. Any restore failure rolls back target writes; nonempty targets
are refused. Backup refuses to overwrite an existing archive. Compare restored
counts and identity with the candidate and retain both command outputs with the
backup. Automated regressions compare restored rows and exercise actual runtime
reopening; operators should also validate representative application data during
the controlled cutover.

For disaster recovery, restore the matching filesystem snapshot and SQL archive,
restore the DSN secret separately, check deployment identity, and only then start
one process. SQL restore alone does not recover local memory, attachments,
configuration, secrets, scratch state or other file-backed services. Filesystem
ZIP backup alone does not recover PG data.

## Validation and remaining limits

Tests use isolated PostgreSQL schemas; they do not migrate existing deployments.
The platform workflow runs real PostgreSQL tests on Linux and default-local-path
regressions on Windows. Multi-instance coordination, online switching, automatic
legacy discovery, file-only fork/checkpoint support and performance qualification
remain outside this version. Search/REPL cost grows with history size; size pools
for the number of loaded workspaces.

macOS validation (Python 3.11, PostgreSQL 15): 2043 passed, 9 skipped across
the affected storage, runtime, workspace, route, checkpoint and doctor suites.

Linux validation (Python 3.11, PostgreSQL 15, Linux aarch64 container): 952 passed,
8 skipped across storage, chats, cron, usage, backup and session hooks. Windows
must have a successful workflow run before Windows verification is claimed.
