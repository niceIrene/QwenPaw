# -*- coding: utf-8 -*-
"""Exercise the runtime session API against both real database backends."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.chats.database_session import DatabaseSession
from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.app.workspace.service_factories import create_session_service
from qwenpaw.config.config import Config
from qwenpaw.runtime._state_utils import StateProxy
from qwenpaw.storage.errors import MigrationConflictError, StorageError
from qwenpaw.storage.models import StorageScope


def make_session(db):
    return DatabaseSession(
        db.config,
        db.root,
        StorageScope("tenant", "workspace", "agent"),
    )


@pytest.mark.asyncio
async def test_runtime_session_reopens_and_updates_state(db):
    session = make_session(db)
    await session.start()
    proxy = StateProxy()
    proxy.data = {"state": {"context": ["你好"]}}
    try:
        await session.save_session_state("chat", agent=proxy)
    finally:
        await session.close()
    reopened = make_session(db)
    await reopened.start()
    restored = StateProxy()
    try:
        await reopened.load_session_state("chat", agent=restored)
        assert restored.data == proxy.data
        await reopened.update_session_state("chat", "agent.mode", "plan")
        state = await reopened.get_session_state_dict("chat")
        assert state["agent"]["mode"] == "plan"
        assert state["agent"]["state"] == proxy.data["state"]
    finally:
        await reopened.close()
    await reopened.close()
    with pytest.raises(StorageError, match="not open"):
        await reopened.get_session_state_dict("chat")


@pytest.mark.asyncio
async def test_runtime_request_revisions_are_not_shared(db):
    session = make_session(db)
    await session.start()
    proxy = StateProxy()
    proxy.data = {"turn": 0}
    await session.save_session_state("chat", agent=proxy)
    ready = asyncio.Event()
    saved = asyncio.Event()

    async def stale_request():
        await session.get_session_state_dict("chat")
        ready.set()
        await saved.wait()
        with pytest.raises(MigrationConflictError):
            await session.save_session_state("chat", agent=proxy)

    async def winning_request():
        await ready.wait()
        await session.get_session_state_dict("chat")
        winner = StateProxy()
        winner.data = {"turn": 1}
        await session.save_session_state("chat", agent=winner)
        saved.set()

    try:
        await asyncio.gather(stale_request(), winning_request())
        assert await session.get_session_state_dict("chat") == {
            "agent": {"turn": 1},
        }
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_workspace_sqlite_default_does_not_open_sql(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("qwenpaw.config.utils.load_config", Config)
    start = AsyncMock()
    monkeypatch.setattr(DatabaseSession, "start", start)
    published = []
    service = await create_session_service(
        SimpleNamespace(workspace_dir=tmp_path, agent_id="default"),
        None,
        published.append,
    )
    assert isinstance(service, SafeJSONSession)
    assert published == [service]
    start.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_postgres_failure_does_not_fall_back(
    tmp_path,
    monkeypatch,
):
    config = Config.model_validate(
        {
            "storage": {
                "backend": "postgresql",
                "deployment_id": "deployment",
            },
        },
    )
    monkeypatch.setattr("qwenpaw.config.utils.load_config", lambda: config)
    published = []

    async def fail_start(service):
        assert published == [service]
        assert service.scope == StorageScope("deployment", "agent", "agent")
        raise StorageError("database unavailable")

    monkeypatch.setattr(DatabaseSession, "start", fail_start)
    with pytest.raises(StorageError, match="database unavailable"):
        await create_session_service(
            SimpleNamespace(workspace_dir=tmp_path, agent_id="agent"),
            None,
            published.append,
        )
    assert len(published) == 1
    assert isinstance(published[0], DatabaseSession)
    assert not (tmp_path / "sessions").exists()


@pytest.mark.asyncio
async def test_failed_session_start_closes_pool(db, monkeypatch):
    session = make_session(db)
    from qwenpaw.storage.factory import StorageRuntime

    monkeypatch.setattr(
        StorageRuntime,
        "sessions",
        AsyncMock(side_effect=StorageError("cannot bind session")),
    )
    closed = []
    original = StorageRuntime.close

    async def record_close(runtime):
        closed.append(runtime)
        await original(runtime)

    monkeypatch.setattr(StorageRuntime, "close", record_close)
    with pytest.raises(StorageError, match="cannot bind"):
        await session.start()
    assert len(closed) == 1
    assert session.runtime is None
    await session.close()
