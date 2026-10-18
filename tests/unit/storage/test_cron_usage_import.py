# -*- coding: utf-8 -*-
"""Offline cron and usage imports preserve data and reject occupied targets."""

import asyncio
import json
from datetime import date

import pytest
from click.testing import CliRunner

from qwenpaw.app.crons.repo.database_repo import DatabaseJobRepository
from qwenpaw.cli.storage_cmd import storage_group
from qwenpaw.storage.errors import StorageConflictError, StorageIdentityError
from qwenpaw.storage.migration.cron import import_cron
from qwenpaw.storage.migration.usage import import_usage
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.records import Records, encode
from qwenpaw.storage.repositories.usage import UsageRepository
from qwenpaw.token_usage.database_buffer import DatabaseTokenUsageBuffer
from qwenpaw.token_usage.manager import TokenUsageManager


def write_cron_source(root):
    (root / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j",
                        "name": "old reminder",
                        "task_type": "text",
                        "text": "hello",
                        "schedule": {"type": "cron", "cron": "0 0 * * *"},
                        "dispatch": {
                            "channel": "console",
                            "target": {"user_id": "u", "session_id": "s"},
                        },
                    },
                ],
            },
        ),
    )
    (root / "j.json").write_text(
        json.dumps(
            [
                {
                    "run_at": "2026-10-17T00:00:00Z",
                    "status": "cancelled",
                    "trigger": "manual",
                },
            ],
        ),
    )
    path = root / "cron-import.json"
    path.write_text(
        json.dumps(
            {
                "workspace_id": "agent",
                "agent_id": "agent",
                "jobs": "jobs.json",
                "history": {"j": "j.json"},
            },
        ),
    )
    return path


def write_usage_source(root):
    data = {
        "2026-10-17": {
            "p:old-model": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "call_count": 2,
                "provider_id": "p",
                "cache_read_tokens": 3,
                "cache_eligible_input_tokens": 10,
                "cache_observed_calls": 1,
            },
        },
    }
    path = root / "usage.json"
    path.write_text(json.dumps(data))
    return path, data


async def test_cron_import_preserves_legacy_defaults_and_history(db, tmp_path):
    path = write_cron_source(tmp_path)
    originals = {p: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert await import_cron(db, path, tenant_id="tenant", epoch=1) == 1
    repo = DatabaseJobRepository(
        Records(db, "tenant", 1),
        StorageScope("tenant", "agent", "agent"),
    )
    assert (await repo.get_job("j")).runtime.share_session is True
    assert (await repo.get_history("j"))[0].status == "cancelled"
    with pytest.raises(StorageConflictError):
        await import_cron(db, path, tenant_id="tenant", epoch=1)
    for source, content in originals.items():
        assert source.read_bytes() == content


async def test_cron_history_conflict_rolls_back_job_registry(db, tmp_path):
    path = write_cron_source(tmp_path)
    records = Records(db, "tenant", 1)
    key = encode(["agent", "agent"])
    await records.put("job_history", key, {"existing": []})
    with pytest.raises(StorageConflictError):
        await import_cron(db, path, tenant_id="tenant", epoch=1)
    assert await records.get("jobs", key) is None
    assert (await records.get("job_history", key))[0] == {"existing": []}


async def test_usage_import_preserves_legacy_identity_and_query_totals(
    db,
    tmp_path,
):
    path, data = write_usage_source(tmp_path)
    original = path.read_bytes()
    assert await import_usage(db, path, tenant_id="tenant", epoch=1) == 1
    repo = UsageRepository(db, "tenant", 1)
    assert await repo.load("2026-10-17", "2026-10-17") == data
    manager = TokenUsageManager()
    # pylint: disable-next=protected-access
    manager._buffer = DatabaseTokenUsageBuffer(repo)
    day = date(2026, 10, 17)
    details = await manager.get_details(day, day)
    assert details[0].agent_id is None
    assert details[0].model == "old-model"
    await manager.record("p", "new-model", 5, 1, at_date=day)
    summary = await manager.get_summary(day, day)
    assert summary.total_calls == 3
    assert summary.total_prompt_tokens == 15
    with pytest.raises(StorageConflictError):
        await import_usage(db, path, tenant_id="tenant", epoch=1)
    assert path.read_bytes() == original


async def test_invalid_usage_does_not_create_an_import_marker(db, tmp_path):
    path, data = write_usage_source(tmp_path)
    data["2026-10-17"]["p:old-model"]["call_count"] = -1
    path.write_text(json.dumps(data))
    with pytest.raises(StorageIdentityError):
        await import_usage(db, path, tenant_id="tenant", epoch=1)
    path, _ = write_usage_source(tmp_path)
    await import_usage(db, path, tenant_id="tenant", epoch=1)


async def test_cli_offline_cron_and_usage_import(db, tmp_path):
    if db.config.backend.value != "postgresql":
        pytest.skip("Application imports target PostgreSQL")
    cron = write_cron_source(tmp_path)
    usage, _ = write_usage_source(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"storage": db.config.model_dump(mode="json")}),
    )
    runner = CliRunner()
    for command, source, field in (
        ("import-cron", cron, "jobs"),
        ("import-usage", usage, "days"),
    ):
        args = ["--config", str(config), command, str(source)]
        missing = await asyncio.to_thread(runner.invoke, storage_group, args)
        assert missing.exit_code != 0
        result = await asyncio.to_thread(
            runner.invoke,
            storage_group,
            [*args, "--offline"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)[field] == 1
