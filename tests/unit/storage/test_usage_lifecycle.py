# -*- coding: utf-8 -*-
"""Application usage selects PG explicitly and closes its own pool."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from qwenpaw.storage.config import StorageConfig
from qwenpaw.storage.errors import StorageError
from qwenpaw.token_usage.database_buffer import DatabaseTokenUsageBuffer
from qwenpaw.token_usage.manager import TokenUsageManager


async def test_configured_usage_restart_preserves_startup_and_shutdown_events(
    db,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("qwenpaw.token_usage.manager.WORKING_DIR", tmp_path)
    manager = TokenUsageManager()
    day = date(2026, 10, 17)
    # Events may arrive during application bootstrap, before backend selection.
    await manager.record("p", "m", 10, 5, at_date=day)
    await manager.start_configured(db.config, tmp_path)
    try:
        summary = await manager.get_summary(day, day)
        assert summary.total_calls == 1
        await manager.record("p", "m", 20, 7, at_date=day)
    finally:
        await manager.stop()
    # Reusing the manager must reopen storage, not keep a closed pool.
    await manager.start_configured(db.config, tmp_path)
    try:
        summary = await manager.get_summary(day, day)
        assert summary.total_calls == 2
        assert summary.total_prompt_tokens == 30
        assert summary.total_completion_tokens == 12
    finally:
        await manager.stop()
    if db.config.backend.value == "postgresql":
        assert not list(tmp_path.glob("*.json"))


async def test_pg_stop_closes_pool_even_when_final_flush_fails(
    db,
    tmp_path,
    monkeypatch,
):
    if db.config.backend.value != "postgresql":
        pytest.skip("PG-specific application lifecycle")
    monkeypatch.setattr("qwenpaw.token_usage.manager.WORKING_DIR", tmp_path)
    manager = TokenUsageManager()
    await manager.start_configured(db.config, tmp_path)
    buffer = manager._buffer  # pylint: disable=protected-access
    assert isinstance(buffer, DatabaseTokenUsageBuffer)
    monkeypatch.setattr(
        buffer.repository,
        "append_batch",
        AsyncMock(side_effect=StorageError("database unavailable")),
    )
    await manager.record("p", "m", 1, 1)
    with pytest.raises(StorageError, match="unavailable"):
        await manager.stop()
    with pytest.raises(StorageError):
        await buffer.repository.load("2026-01-01", "2026-12-31")


async def test_pg_start_failure_does_not_fall_back_to_local_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("qwenpaw.token_usage.manager.WORKING_DIR", tmp_path)
    monkeypatch.delenv("QWENPAW_MISSING_TEST_DSN", raising=False)
    manager = TokenUsageManager()
    config = StorageConfig(
        backend="postgresql",
        deployment_id="test",
        postgresql={"dsn_env": "QWENPAW_MISSING_TEST_DSN"},
    )
    with pytest.raises(ValueError, match="Missing database environment"):
        await manager.start_configured(config, tmp_path)
    assert not list(tmp_path.iterdir())
