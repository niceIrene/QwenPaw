# -*- coding: utf-8 -*-
"""Recall snapshots preserve identities and never mix committed versions."""

from contextlib import aclosing, closing

import pytest

from qwenpaw.agents.context.scroll.memoryspace import MemorySpace
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.storage.backends.sqlite.recall_snapshot import (
    refresh_history_snapshot,
)
from qwenpaw.storage.errors import StorageIdentityError
from qwenpaw.storage.backends.sqlite.legacy_history import SQLiteHistoryStore
from qwenpaw.storage.repositories.history import SqlHistoryStore


async def history_for(db, tenant="tenant"):
    return await SqlHistoryStore.open(
        db,
        tenant_id=tenant,
        workspace_id="workspace",
        agent_id="agent",
        epoch=1,
    )


@pytest.mark.asyncio
async def test_snapshot_preserves_sequences_scope_search_and_refresh(
    db,
    tmp_path,
):
    history = await history_for(db)
    other = await history_for(db, "other")
    await other.append(
        session_id="s",
        entry=LogEntry(kind="model_turn", content="secret"),
    )
    for text in ("deleted", "tanks", "old"):
        await history.append(
            session_id="s",
            entry=LogEntry(kind="model_turn", content=text),
        )
    async with db.transaction(write=True) as tx:
        await tx.execute(
            f"DELETE FROM {db.table('history')} "
            f"WHERE store_id={db.bind(1)} AND seq=1",
            history.store_id,
        )
    path = tmp_path / "recall.db"
    await refresh_history_snapshot(history, path)
    with closing(MemorySpace(history_db_path=path)) as ms:
        assert ms.sql_query(
            "SELECT seq, content FROM hist.conversation_history ORDER BY seq",
        ) == [
            {"seq": 2, "content": "tanks"},
            {"seq": 3, "content": "old"},
        ]
        assert ms.sql_query(
            "SELECT rowid FROM hist.conversation_history_fts "
            "WHERE conversation_history_fts MATCH 'tank'",
        ) == [{"rowid": 2}]
    await history.update_entry(3, content="updated")
    await refresh_history_snapshot(history, path)
    with closing(MemorySpace(history_db_path=path)) as ms:
        assert ms.sql_query(
            "SELECT content FROM hist.conversation_history WHERE seq=3",
        ) == [{"content": "updated"}]
    history.epoch += 1
    with pytest.raises(StorageIdentityError):
        await refresh_history_snapshot(history, path)
    assert not list(tmp_path.glob(".snapshot-*"))
    # Disposable snapshot paths must not accumulate in the integrity cache.
    assert not any(
        path.is_relative_to(tmp_path)
        # pylint: disable-next=protected-access
        for _, path in SQLiteHistoryStore._integrity_probe_checked
    )


@pytest.mark.asyncio
async def test_postgres_snapshot_does_not_mix_concurrent_updates(db):
    if db.config.backend != "postgresql":
        pytest.skip("Concurrent writer requires independent PG connections")
    history = await history_for(db)
    for text in ("first", "second"):
        await history.append(
            session_id="s",
            entry=LogEntry(kind="model_turn", content=text),
        )
    async with aclosing(history.snapshot_pages(page_size=1)) as pages:
        assert (await anext(pages))[0]["content"] == "first"
        await history.update_entry(2, content="changed")
        assert (await anext(pages))[0]["content"] == "second"
    assert await history.contents_by_seqs({2}) == {2: "changed"}


@pytest.mark.asyncio
async def test_filtered_snapshots_preserve_lineage_and_truncation(
    db,
    tmp_path,
):
    history = await history_for(db)
    for index, sid in enumerate(("s", "other", "s", "s", "s", "s")):
        await history.append(
            session_id=sid,
            entry=LogEntry(
                kind="tool_result",
                content=f"row {index}",
                tool_call_id="wanted" if index < 5 else "unrelated",
            ),
        )
    # Legacy imports can contain unassigned or differently attributed rows.
    async with db.transaction(write=True) as tx:
        await tx.execute(
            f"UPDATE {db.table('history')} SET agent_id=NULL WHERE seq=1",
        )
        await tx.execute(
            f"UPDATE {db.table('history')} SET agent_id='other' WHERE seq=3",
        )
    full, filtered = tmp_path / "full.db", tmp_path / "filtered.db"
    await refresh_history_snapshot(history, full)
    with closing(
        MemorySpace(
            history_db_path=full,
            session_id="s",
            agent_id="agent",
            row_cap=2,
        ),
    ) as ms:
        expected_expand = ms.expand(1, 6)
        expected_tool = ms.recall_tool("wanted")
    for selection, method, args, expected in (
        (
            {
                "after": 0,
                "through": 6,
                "session_id": "s",
                "include_unassigned_agent": True,
            },
            "expand",
            (1, 6),
            expected_expand,
        ),
        (
            {"tool_call_id": "wanted"},
            "recall_tool",
            ("wanted",),
            expected_tool,
        ),
    ):
        await refresh_history_snapshot(
            history,
            filtered,
            search_index=False,
            page_size=2,
            agent_id="agent",
            limit=3,
            **selection,
        )
        with closing(
            MemorySpace(
                history_db_path=filtered,
                session_id="s",
                agent_id="agent",
                row_cap=2,
            ),
        ) as ms:
            assert getattr(ms, method)(*args) == expected
            assert expected[-1] == {"_truncated": True, "_row_cap": 2}
            assert ms.sql_query(
                "SELECT COUNT(*) AS n FROM hist.conversation_history",
            ) == [{"n": 3}]
            assert (
                ms.sql_query(
                    "SELECT name FROM hist.sqlite_master "
                    "WHERE name='conversation_history_fts'",
                )
                == []
            )
