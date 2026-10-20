# -*- coding: utf-8 -*-
"""Offline import preserves runtime identities and rolls back partial work."""

import json

import pytest

from qwenpaw.agents.context.types import LogEntry
from qwenpaw.app.chats.models import ChatsFile, ChatSpec
from qwenpaw.app.chats.repo.database_repo import DatabaseChatRepository
from qwenpaw.storage.backends.sqlite.legacy_history import SQLiteHistoryStore
from qwenpaw.storage.errors import StorageConflictError, StorageIdentityError
from qwenpaw.storage.migration.workspace import import_workspace
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.history import SqlHistoryStore
from qwenpaw.storage.repositories.records import Records
from qwenpaw.storage.repositories.sessions import SessionKey


@pytest.fixture(name="legacy_workspace")
def make_legacy_workspace(tmp_path):
    history = SQLiteHistoryStore(tmp_path / "history.db")
    history.append(
        session_id="s",
        entry=LogEntry(kind="model_turn", content="original"),
    )
    history.close()
    (tmp_path / "session.json").write_text('{"agent":{"scroll":{"seq":1}}}')
    chats = ChatsFile(
        chats=[ChatSpec(session_id="s", user_id="u", channel="c")],
    )
    (tmp_path / "chats.json").write_text(chats.model_dump_json())
    manifest = {
        "workspace_id": "agent",
        "agent_id": "agent",
        "history": "history.db",
        "chats": "chats.json",
        "sessions": [
            {
                "session_id": "s",
                "user_id": "u",
                "channel": "c",
                "path": "session.json",
            },
        ],
    }
    path = tmp_path / "import.json"
    path.write_text(json.dumps(manifest))
    return path, manifest


async def test_import_roundtrip_and_occupied_target(db, legacy_workspace):
    path, _ = legacy_workspace
    originals = {
        p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()
    }
    await import_workspace(db, path, tenant_id="tenant", epoch=1)
    store = await SqlHistoryStore.open(
        db,
        tenant_id="tenant",
        workspace_id="agent",
        agent_id="agent",
        epoch=1,
    )
    key = SessionKey("s", "u", "c").encode()
    assert await store.load_checkpoint(key) == (
        {"agent": {"scroll": {"seq": 1}}},
        1,
    )
    pages = [page async for page in store.snapshot_pages(session_id=key)]
    assert len(pages) == 1
    assert pages[0][0]["content"] == "original"
    repo = DatabaseChatRepository(
        Records(db, "tenant", 1),
        StorageScope("tenant", "agent", "agent"),
    )
    assert (await repo.load()).chats[0].session_id == "s"
    with pytest.raises(StorageConflictError):
        await import_workspace(db, path, tenant_id="tenant", epoch=1)
    for source, content in originals.items():
        assert source.read_bytes() == content


async def test_import_unmapped_history_rolls_back_metadata(
    db,
    legacy_workspace,
):
    path, manifest = legacy_workspace
    manifest["sessions"][0]["session_id"] = "different"
    path.write_text(json.dumps(manifest))
    (path.parent / "chats.json").write_text("{}")
    with pytest.raises(StorageIdentityError, match="unmapped session ID"):
        await import_workspace(db, path, tenant_id="tenant", epoch=1)
    async with db.transaction() as tx:
        for table in ("stores", "records", "checkpoints", "history"):
            assert await tx.one(f"SELECT * FROM {db.table(table)}") is None
    # Correcting the manifest permits a clean retry without recovery state.
    manifest["sessions"][0]["session_id"] = "s"
    path.write_text(json.dumps(manifest))
    await import_workspace(db, path, tenant_id="tenant", epoch=1)


async def test_import_sessions_without_history(db, legacy_workspace):
    path, manifest = legacy_workspace
    manifest.pop("history")
    path.write_text(json.dumps(manifest))
    await import_workspace(db, path, tenant_id="tenant", epoch=1)
    store = await SqlHistoryStore.open(
        db,
        tenant_id="tenant",
        workspace_id="agent",
        agent_id="agent",
        epoch=1,
    )
    assert await store.load_checkpoint(SessionKey("s", "u", "c").encode())


async def test_import_rejects_ambiguous_history_identity(db, legacy_workspace):
    path, manifest = legacy_workspace
    manifest["sessions"].append(
        {"session_id": "s", "user_id": "another", "channel": "c"},
    )
    path.write_text(json.dumps(manifest))
    with pytest.raises(StorageIdentityError, match="Ambiguous"):
        await import_workspace(db, path, tenant_id="tenant", epoch=1)


async def test_import_cli_requires_offline_and_uses_configured_scope(
    db,
    legacy_workspace,
):
    import asyncio
    from click.testing import CliRunner
    from qwenpaw.cli.storage_cmd import storage_group

    if db.config.backend.value != "postgresql":
        pytest.skip("Application import targets PostgreSQL only")
    path, _ = legacy_workspace
    config = path.parent / "config.json"
    config.write_text(
        json.dumps({"storage": db.config.model_dump(mode="json")}),
    )
    args = ["--config", str(config), "import-workspace", str(path)]
    runner = CliRunner()
    missing = await asyncio.to_thread(runner.invoke, storage_group, args)
    assert missing.exit_code != 0
    result = await asyncio.to_thread(
        runner.invoke,
        storage_group,
        [*args, "--offline"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["imported"] is True
    records = Records(db, db.config.deployment_id, 1)
    repo = DatabaseChatRepository(
        records,
        StorageScope(db.config.deployment_id, "agent", "agent"),
    )
    assert (await repo.load()).chats[0].session_id == "s"


async def test_checkpoint_references_require_history_and_report_gaps(
    db,
    legacy_workspace,
):
    path, manifest = legacy_workspace
    checkpoint = {
        "agent": {
            "scroll": {
                "persisted_ids": ["present", "expired"],
                "seq_by_id": {"present": [1, 1], "expired": [99, 99]},
            },
        },
    }
    (path.parent / "session.json").write_text(json.dumps(checkpoint))
    manifest.pop("history")
    path.write_text(json.dumps(manifest))
    with pytest.raises(StorageIdentityError, match="no history"):
        await import_workspace(db, path, tenant_id="tenant", epoch=1)
    async with db.transaction() as tx:
        assert await tx.one(f"SELECT * FROM {db.table('stores')}") is None
    manifest["history"] = "history.db"
    path.write_text(json.dumps(manifest))
    with pytest.raises(StorageIdentityError, match="reference report"):
        await import_workspace(db, path, tenant_id="tenant", epoch=1)
    report = []
    await import_workspace(
        db,
        path,
        tenant_id="tenant",
        epoch=1,
        reference_report=report,
    )
    assert report == [
        {
            "session_key": SessionKey("s", "u", "c").encode(),
            "checked_sequence_endpoints": 2,
            "missing_sequences": [99],
            "unmapped_message_ids": [],
            "unmapped_tool_ids": [],
            "requires_review": True,
        },
    ]
