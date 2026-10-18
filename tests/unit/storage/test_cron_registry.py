# -*- coding: utf-8 -*-
"""Cron definitions and retained history persist in the selected workspace."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.chats.database_session import DatabaseSession
from qwenpaw.app.crons.manager import CronManager
from qwenpaw.app.crons.models import CronExecutionRecord, CronJobSpec
from qwenpaw.app.crons.repo.database_repo import DatabaseJobRepository
from qwenpaw.app.crons.repo.json_repo import JsonJobRepository
from qwenpaw.app.workspace.service_factories import create_job_repository
from qwenpaw.storage.errors import StorageConflictError
from qwenpaw.storage.factory import StorageRuntime
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.records import Records


def repository(db, workspace="workspace"):
    return DatabaseJobRepository(
        Records(db, "tenant", 1),
        StorageScope("tenant", workspace, "agent"),
    )


def job():
    return CronJobSpec.model_validate(
        {
            "id": "job",
            "name": "Reminder",
            "enabled": False,
            "task_type": "text",
            "text": "hello",
            "schedule": {"type": "cron", "cron": "0 0 * * *"},
            "dispatch": {
                "channel": "console",
                "target": {"user_id": "u", "session_id": "s"},
            },
        },
    )


async def test_jobs_persist_and_reject_stale_snapshots(db):
    repo = repository(db)
    stale = await repo.load()
    spec = job()
    await repo.upsert_job(spec)
    assert (await repository(db).get_job("job")) == spec
    with pytest.raises(StorageConflictError):
        await repo.save(stale)
    assert await repository(db, "another").list_jobs() == []


async def test_job_history_retention_concurrent_append_and_pruning(db):
    repo = repository(db)
    record = CronExecutionRecord(
        run_at=datetime.now(timezone.utc),
        status="success",
    )
    await asyncio.gather(
        *[repo.append_history("job", record, limit=2) for _ in range(3)],
    )
    assert len(await repository(db).get_history("job")) == 2
    await repo.append_history("orphan", record)
    await repo.prune_orphan_history({"job"})
    assert await repo.get_history("orphan") == []
    assert len(await repo.get_history("job")) == 2
    assert await repository(db, "another").get_history("job") == []
    await repo.delete_history("job")
    assert await repository(db).get_history("job") == []


async def test_workspace_factory_and_cancelled_execution_persistence(
    db,
    tmp_path,
    monkeypatch,
):
    ws = SimpleNamespace(
        workspace_dir=tmp_path,
        session=None,
        memory_manager=None,
    )
    assert isinstance(create_job_repository(ws), JsonJobRepository)
    ws.session = DatabaseSession(
        db.config,
        tmp_path,
        StorageScope("tenant", "workspace", "agent"),
    )
    ws.session.runtime = StorageRuntime(db, 1)
    repo = create_job_repository(ws)
    assert isinstance(repo, DatabaseJobRepository)
    await repo.upsert_job(job())
    manager = CronManager(repo=repo, workspace=ws, channel_manager=AsyncMock())
    monkeypatch.setattr(
        "qwenpaw.app.crons.manager.get_heartbeat_config",
        lambda _: SimpleNamespace(enabled=False),
    )
    executing = asyncio.Event()

    async def execute(_job):
        executing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        manager._executor,  # pylint: disable=protected-access
        "execute",
        execute,
    )
    await manager.start()
    try:
        await manager.run_job("job")
        await asyncio.wait_for(executing.wait(), timeout=2)
    finally:
        await manager.stop()
    assert (await repository(db).get_history("job"))[0].status == "cancelled"
    assert not (tmp_path / "jobs.json").exists()
    assert not (tmp_path / "jobs_history").exists()
