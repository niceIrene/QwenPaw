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
