# -*- coding: utf-8 -*-
"""Remote history must commit before scroll bookkeeping and checkpoints."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.storage.errors import StorageError
from qwenpaw.storage.factory import create_storage
from qwenpaw.storage.models import StorageScope


def make_agent():
    message = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text="first")],
    )
    return SimpleNamespace(state=SimpleNamespace(context=[message]))


@pytest.mark.asyncio
async def test_async_scroll_defers_and_awaits_writes(db, monkeypatch):
    async with create_storage(db.config, db.root) as runtime:
        history = await runtime.history(StorageScope("tenant", "ws", "agent"))
        manager = ScrollContextManager(
            history=history,
            session_id="chat",
            agent_id="agent",
        )
        agent = make_agent()
        message = agent.state.context[0]
        manager.on_save(agent, message.content)
        assert await history.count("chat") == 0

        started, release = asyncio.Event(), asyncio.Event()
        original_append = history.append

        async def delayed_append(**kwargs):
            started.set()
            await release.wait()
            return await original_append(**kwargs)

        monkeypatch.setattr(history, "append", delayed_append)
        pending = asyncio.create_task(manager.flush(agent))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert manager.to_dict()["seq_by_id"] == {}
        finally:
            release.set()
            await pending
        sequence = (await history.rows())[0]["seq"]
        message.content.append(TextBlock(type="text", text="second"))
        manager.on_save(agent, message.content)
        await manager.flush(agent)
        rows = await history.rows()
        assert len(rows) == 1
        assert rows[0]["seq"] == sequence
        assert "second" in rows[0]["content"]
        await manager.aclose()


@pytest.mark.asyncio
async def test_async_scroll_failed_write_cannot_be_checkpointed(
    db,
    monkeypatch,
):
    async with create_storage(db.config, db.root) as runtime:
        history = await runtime.history(StorageScope("tenant", "ws", "agent"))
        manager = ScrollContextManager(
            history=history,
            session_id="chat",
            agent_id="agent",
        )
        agent = make_agent()
        original_append = history.append
        monkeypatch.setattr(
            history,
            "append",
            AsyncMock(side_effect=StorageError("offline")),
        )
        with pytest.raises(StorageError, match="uncommitted"):
            await manager.flush(agent)
        assert manager.to_dict()["persisted_ids"] == []
        assert await history.count("chat") == 0
        monkeypatch.setattr(history, "append", original_append)
        await manager.flush(agent)
        assert await history.count("chat") == 1
        await manager.aclose()
        with pytest.raises(StorageError, match="uncommitted"):
            await manager.flush(agent)
