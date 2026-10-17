# -*- coding: utf-8 -*-
"""Usage retries preserve counts, scope and events arriving during I/O."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from qwenpaw.storage.errors import StorageConflictError
from qwenpaw.storage.repositories.usage import UsageRepository
from qwenpaw.token_usage.buffer import _UsageEvent, _apply_event
from qwenpaw.token_usage.database_buffer import DatabaseTokenUsageBuffer


DAY = "2026-10-17"
KEY = "agent\x1fprovider\x1fmodel"


def event(day=DAY):
    return _UsageEvent(
        "provider",
        "model",
        10,
        4,
        day,
        f"{day}T00:00:00Z",
        cache_read_tokens=3,
        cache_eligible_input_tokens=10,
        cache_observed=True,
        agent_id="agent",
    )


def batch(day=DAY):
    delta = {}
    _apply_event(delta, event(day))
    return delta


async def test_batch_retry_concurrent_merge_and_date_tenant_scope(db):
    repo = UsageRepository(db, "tenant", 1)
    await asyncio.gather(
        repo.append_batch("one", batch()),
        repo.append_batch("two", batch()),
    )
    await repo.append_batch("one", batch())
    with pytest.raises(StorageConflictError):
        await repo.append_batch("one", batch("2026-10-16"))
    await repo.append_batch("older", batch("2026-10-16"))
    totals = await repo.load(DAY, DAY)
    assert list(totals) == [DAY]
    assert totals[DAY][KEY]["call_count"] == 2
    assert totals[DAY][KEY]["prompt_tokens"] == 20
    assert totals[DAY][KEY]["cache_read_tokens"] == 6
    assert totals[DAY][KEY]["cache_observed_calls"] == 2
    assert await UsageRepository(db, "other", 1).load(DAY, DAY) == {}


async def test_buffer_retries_committed_batch_after_lost_response(
    db,
    monkeypatch,
):
    repo = UsageRepository(db, "tenant", 1)
    buffer = DatabaseTokenUsageBuffer(repo)
    original = repo.append_batch

    async def commit_then_fail(batch_id, delta):
        await original(batch_id, delta)
        raise OSError("simulated lost commit response")

    monkeypatch.setattr(repo, "append_batch", commit_then_fail)
    buffer.enqueue(event())
    with pytest.raises(OSError):
        await buffer.flush()
    buffer.enqueue(event())
    monkeypatch.setattr(repo, "append_batch", original)
    await buffer.stop()
    totals = await repo.load(DAY, DAY)
    assert totals[DAY][KEY]["call_count"] == 2
    assert totals[DAY][KEY]["prompt_tokens"] == 20


async def test_cancelled_batch_is_retained_for_final_flush(db, monkeypatch):
    repo = UsageRepository(db, "tenant", 1)
    buffer = DatabaseTokenUsageBuffer(repo)
    original = repo.append_batch
    monkeypatch.setattr(
        repo,
        "append_batch",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    buffer.enqueue(event())
    with pytest.raises(asyncio.CancelledError):
        await buffer.flush()
    monkeypatch.setattr(repo, "append_batch", original)
    await buffer.stop()
    assert (await repo.load(DAY, DAY))[DAY][KEY]["call_count"] == 1


async def test_query_includes_arrivals_during_read_without_double_counting(
    db,
    monkeypatch,
):
    repo = UsageRepository(db, "tenant", 1)
    buffer = DatabaseTokenUsageBuffer(repo)
    original = repo.load

    async def load_with_new_event(start, end):
        result = await original(start, end)
        buffer.enqueue(event())
        return result

    buffer.enqueue(event())
    monkeypatch.setattr(repo, "load", load_with_new_event)
    totals = await buffer.get_merged_data(DAY, DAY)
    assert totals[DAY][KEY]["call_count"] == 2
    monkeypatch.setattr(repo, "load", original)
    assert (await buffer.get_merged_data(DAY, DAY))[DAY][KEY][
        "call_count"
    ] == 2
