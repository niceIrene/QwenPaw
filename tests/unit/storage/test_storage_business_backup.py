# -*- coding: utf-8 -*-
"""SQL backups round-trip all owned tables and fail without partial restore."""

import asyncio
import json
import os
import threading
from uuid import uuid4

import pytest

from qwenpaw.agents.context.types import LogEntry
from qwenpaw.storage import backup as backup_module
from qwenpaw.storage.backup import backup_storage, restore_storage
from qwenpaw.storage.contracts.database import TABLES
from qwenpaw.storage.errors import StorageConflictError, StorageIdentityError
from qwenpaw.storage.factory import create_database
from qwenpaw.storage.repositories.history import SqlHistoryStore
from qwenpaw.storage.repositories.records import Records, encode
from qwenpaw.storage.schema import identity, initialize


@pytest.fixture(name="target")
async def make_target(db, tmp_path):
    config = db.config.model_copy(deep=True)
    config.postgresql.schema_name = f"restore_{uuid4().hex}"
    config.postgresql.table_prefix = "restored_"
    target = create_database(config, tmp_path / "target")
    await target.open(create=True)
    await initialize(target)
    try:
        yield target
    finally:
        if config.backend == "postgresql":
            async with target.transaction(write=True) as tx:
                await tx.execute(
                    f'DROP SCHEMA "{config.postgresql.schema_name}" CASCADE',
                )
        await target.close()


async def seed(db):
    history = await SqlHistoryStore.open(
        db,
        tenant_id="tenant",
        workspace_id="workspace",
        agent_id="agent",
        epoch=1,
    )
    for i in range(3):
        await history.append(
            session_id="session",
            entry=LogEntry(kind="model_turn", content=f"message {i}"),
        )
    await history.save_checkpoint("session", {"agent": {"seq": 3}}, revision=0)
    records = Records(db, "tenant", 1)
    for collection in ("chats", "jobs", "job_history", "token_usage"):
        await records.put(collection, "one", {"content": "保留 Unicode 和版本"})
    await records.record_usage("batch", {"calls": 3})
    async with db.transaction(write=True) as tx:
        await tx.execute(
            f"INSERT INTO {db.table('leases')} VALUES ({db.binds(5)})",
            history.store_id,
            "session",
            "stopped-worker",
            7,
            0.0,
        )
        await tx.execute(
            f"INSERT INTO {db.table('migration_jobs')} VALUES ({db.binds(3)})",
            "previous-migration",
            "complete",
            '{"verified":true}',
        )


async def all_rows(db):
    async with db.transaction() as tx:
        return {
            table: sorted(
                encode(row)
                for row in await tx.fetch(f"SELECT * FROM {db.table(table)}")
            )
            for table in TABLES
        }


async def test_backup_roundtrip_and_no_overwrite(
    db,
    target,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(backup_module, "_BATCH_SIZE", 2)
    await seed(db)
    source_rows = await all_rows(db)
    path = tmp_path / "backup.jsonl"
    counts = await backup_storage(db, path)
    assert counts["history"] == 3
    assert counts["records"] == 4
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    assert await restore_storage(target, path, epoch=1) == counts
    assert await all_rows(target) == source_rows
    original = path.read_bytes()
    with pytest.raises(StorageConflictError):
        await backup_storage(db, path)
    assert path.read_bytes() == original
    with pytest.raises(StorageConflictError):
        await restore_storage(target, path, epoch=1)
    assert await all_rows(target) == source_rows


async def test_backup_refuses_live_leases_and_restore_refuses_journals(
    db,
    target,
    tmp_path,
):
    await seed(db)
    path = tmp_path / "offline.jsonl"
    async with db.transaction(write=True) as tx:
        await tx.execute(
            f"UPDATE {db.table('leases')} SET expires_at={db.clock()}+60",
        )
    with pytest.raises(StorageConflictError, match="leases"):
        await backup_storage(db, path)
    assert not path.exists()
    async with db.transaction(write=True) as tx:
        await tx.execute(f"UPDATE {db.table('leases')} SET expires_at=0")
    await backup_storage(db, path)
    async with target.transaction(write=True) as tx:
        await tx.execute(
            f"INSERT INTO {target.table('migration_jobs')} "
            f"VALUES ({target.binds(3)})",
            "target-migration",
            "complete",
            "{}",
        )
    before = await all_rows(target)
    with pytest.raises(StorageConflictError, match="empty"):
        await restore_storage(target, path, epoch=1)
    assert await all_rows(target) == before


@pytest.mark.parametrize("damage", ["checksum", "truncated", "foreign"])
async def test_invalid_backup_rolls_back_rows_and_identity(
    db,
    target,
    tmp_path,
    monkeypatch,
    damage,
):
    monkeypatch.setattr(backup_module, "_BATCH_SIZE", 2)
    await seed(db)
    path = tmp_path / "backup.jsonl"
    await backup_storage(db, path)
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    if damage == "checksum":
        footer = json.loads(lines[-1])
        footer["sha256"] = "0" * 64
        lines[-1] = (json.dumps(footer) + "\n").encode()
    elif damage == "truncated":
        lines.pop()
    else:
        header = json.loads(lines[0])
        header["metadata"]["deployment_id"] = "someone-else"
        lines[0] = (json.dumps(header) + "\n").encode()
    path.write_bytes(b"".join(lines))
    empty_target = await all_rows(target)
    with pytest.raises(StorageIdentityError):
        await restore_storage(target, path, epoch=1)
    assert await all_rows(target) == empty_target
    path.write_bytes(original)
    await restore_storage(target, path, epoch=1)
    assert await identity(target) == await identity(db)


async def test_cancelled_backup_drains_file_writer_without_publishing(
    db,
    tmp_path,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    original = backup_module._write_lines  # pylint: disable=protected-access

    def slow_write(*args):
        entered.set()
        assert release.wait(timeout=5)
        original(*args)

    monkeypatch.setattr(backup_module, "_write_lines", slow_write)
    output = tmp_path / "cancelled.jsonl"
    task = asyncio.create_task(backup_storage(db, output))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not output.exists()
    # A cancelled export has also released its read transaction.
    await Records(db, "tenant", 1).put("chats", "new", {})


async def test_pg_backup_keeps_one_snapshot_across_tables(
    db,
    target,
    tmp_path,
    monkeypatch,
):
    if db.config.backend != "postgresql":
        pytest.skip("Concurrent connection requires PostgreSQL")
    await seed(db)
    expected = await all_rows(db)
    original = backup_module.run_sync_io
    inserted = False

    async def write_then_mutate(operation, *args):
        nonlocal inserted
        result = await original(operation, *args)
        if not inserted:
            inserted = True
            await Records(db, "tenant", 1).put("chats", "after-snapshot", {})
        return result

    monkeypatch.setattr(backup_module, "run_sync_io", write_then_mutate)
    path = tmp_path / "snapshot.jsonl"
    await backup_storage(db, path)
    await restore_storage(target, path, epoch=1)
    assert await all_rows(target) == expected
    assert await Records(db, "tenant", 1).get("chats", "after-snapshot")
