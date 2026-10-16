# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Exercise the application builder through SQL persistence and recall."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.app.chats.database_session import DatabaseSession
from qwenpaw.agents.command_handler import CommandHandler
from qwenpaw.config.config import LightContextConfig
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.storage.errors import StorageError
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.sessions import SessionKey


async def assert_queries_without_storage(scroll, seq):
    """No-op and calendar requests must work even when PG is unavailable."""
    blocked = await scroll.recall_tool(op="expand", lo=seq, hi=seq)
    assert "RECALL LOOP BLOCKED" in str(blocked.content)
    invalid = await scroll.recall_tool(op="expand", lo="bad", hi=seq)
    assert "RECALL FAILED" in str(invalid.content)
    dates = await scroll.recall_tool(
        op="days_between",
        start="2026-10-01",
        end="2026-10-16",
    )
    assert "= 15" in str(dates.content)


@pytest.mark.asyncio
# pylint: disable-next=too-many-statements
async def test_builder_uses_database_history_and_fresh_recall(
    db,
    tmp_path,
    monkeypatch,
):
    session = DatabaseSession(
        db.config,
        db.root,
        StorageScope("tenant", "ws", "agent"),
    )
    await session.start()
    config = SimpleNamespace(
        id="agent",
        running=SimpleNamespace(
            light_context_config=LightContextConfig(strategy="scroll"),
        ),
    )
    config.running.light_context_config.scroll_config.allow_unsandboxed = True
    monkeypatch.setenv("QWENPAW_ALLOW_UNSANDBOXED_RECALL", "1")
    ctx = SimpleNamespace(
        workspace=SimpleNamespace(session=session, workspace_dir=tmp_path),
        session_id="chat",
        request=SimpleNamespace(user_id="user", channel="console"),
    )
    scroll = None
    try:
        scroll = await AgentBuilder._build_scroll_components(ctx, config, None)
        manager = scroll.context_manager
        message = Msg(
            name="assistant",
            content=[TextBlock(type="text", text="stored remotely")],
            role="assistant",
        )
        agent = SimpleNamespace(state=SimpleNamespace(context=[message]))
        await session.repository.history.append(
            session_id="other-session",
            entry=LogEntry(kind="model_turn", content="unrelated history"),
        )
        await manager.flush(agent)
        rows = await session.repository.history.rows(
            session_id=SessionKey("chat", "user", "console").encode(),
        )
        assert (
            rows[0]["session_id"]
            == SessionKey("chat", "user", "console").encode()
        )
        seq = rows[0]["seq"]
        result = await scroll.recall_tool(op="expand", lo=seq, hi=seq)
        assert "stored remotely" in str(result.content)
        assert scroll.recall_tool._tool_descriptor.func is scroll.recall_tool
        assert not (tmp_path / "history.db").exists()
        with closing(sqlite3.connect(manager.snapshot_path)) as view:
            assert (
                view.execute(
                    "SELECT COUNT(*) FROM conversation_history",
                ).fetchone()[0]
                == 1
            )
            assert (
                view.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='conversation_history_fts'",
                ).fetchone()
                is None
            )

        await session.repository.history.update_entry(
            seq,
            content="updated remotely",
        )
        manager._recall_loop_guard.begin_turn("next-user-turn")
        result = await scroll.recall_tool(op="expand", lo=seq, hi=seq)
        assert "updated remotely" in str(result.content)

        result = await scroll.repl_tool(
            source=(
                "print(ms.sql_query("
                "'SELECT content FROM hist.conversation_history'))"
            ),
        )
        assert "updated remotely" in str(result.content)

        assert "unrelated history" in str(result.content)

        monkeypatch.setattr(
            "qwenpaw.agents.context.scroll.database_runtime."
            "refresh_history_snapshot",
            AsyncMock(side_effect=StorageError("offline")),
        )
        # A completed page, invalid arguments and date math need no PG read.
        await assert_queries_without_storage(scroll, seq)
        manager._recall_loop_guard.begin_turn("offline-turn")
        with pytest.raises(StorageError, match="offline"):
            await scroll.recall_tool(op="expand", lo=seq, hi=seq)
        path = manager.snapshot_path
        await manager.aclose()
        assert not path.parent.exists()
        # Closing a request's history must not close the workspace pool.
        assert (
            await session.repository.history.count(rows[0]["session_id"]) == 1
        )
        # Standalone /compact must use the normal turn's store and key.
        command = CommandHandler(
            "agent",
            state=agent.state,
            agent_id="agent",
            workspace_dir=str(tmp_path),
            session_id="chat",
            prompt_context=ctx,
        )
        compact_manager = await command._build_standalone_scroll_manager(
            config,
        )
        try:
            compact_manager.load_state(manager.to_dict())
            await compact_manager.flush(agent)
            assert (
                await session.repository.history.count(rows[0]["session_id"])
                == 1
            )
            assert not (tmp_path / "history.db").exists()
        finally:
            await compact_manager.aclose()
    finally:
        if scroll is not None:
            await scroll.context_manager.aclose()
        await session.close()
