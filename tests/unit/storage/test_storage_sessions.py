# -*- coding: utf-8 -*-
"""Session identity, restart durability and stale-write transaction checks."""

import pytest

from qwenpaw.agents.context.types import LogEntry
from qwenpaw.storage.errors import MigrationConflictError
from qwenpaw.storage.factory import create_storage
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.sessions import SessionKey


@pytest.mark.asyncio
async def test_session_restart_and_identity_isolation(db):
    scope = StorageScope("tenant", "workspace", "agent")
    key = SessionKey("chat:one", "user", "console")
    async with create_storage(db.config, db.root) as runtime:
        sessions = await runtime.sessions(scope)
        assert await sessions.load(key) is None
        assert (
            await sessions.save(key, {"agent": {"text": "你好"}}, revision=0)
            == 1
        )
    async with create_storage(db.config, db.root) as runtime:
        sessions = await runtime.sessions(scope)
        snapshot = await sessions.load(key)
        assert snapshot.state == {"agent": {"text": "你好"}}
        assert snapshot.revision == 1
        for other in (
            SessionKey("chat--one", "user", "console"),
            SessionKey("chat:one", "other", "console"),
            SessionKey("chat:one", "user", "other"),
        ):
            assert await sessions.load(other) is None
        other_scope = await runtime.sessions(
            StorageScope("tenant", "other", "agent"),
        )
        assert await other_scope.load(key) is None


@pytest.mark.asyncio
async def test_stale_session_save_rolls_back_pending_history(db):
    scope = StorageScope("tenant", "workspace", "agent")
    key = SessionKey("chat")
    async with create_storage(db.config, db.root) as runtime:
        writer = await runtime.sessions(scope)
        stale_writer = await runtime.sessions(scope)
        await writer.save(key, {"turn": 1}, revision=0)
        snapshot = await stale_writer.load(key)
        await writer.save(key, {"turn": 2}, revision=1)
        with pytest.raises(MigrationConflictError):
            await stale_writer.save(
                key,
                {"turn": "stale"},
                revision=snapshot.revision,
                entries=[
                    (LogEntry(kind="message", content="uncommitted"), "event"),
                ],
            )
        assert await writer.history.count(key.encode()) == 0
        assert (await writer.load(key)).state == {"turn": 2}
        await writer.save(
            key,
            {"turn": 3},
            revision=2,
            entries=[(LogEntry(kind="message", content="committed"), "event")],
        )
        assert await writer.history.count(key.encode()) == 1
