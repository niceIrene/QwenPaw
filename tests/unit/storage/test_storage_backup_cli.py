# -*- coding: utf-8 -*-
"""Exercise the offline backup/restore commands with a real PG namespace."""

import asyncio
import json

import pytest
from click.testing import CliRunner

from qwenpaw.cli.storage_cmd import storage_group
from qwenpaw.storage.repositories.records import Records


async def test_cli_pg_backup_and_restore(db, tmp_path):
    if db.config.backend != "postgresql":
        pytest.skip("Application backup commands target PostgreSQL")
    config = tmp_path / "pg.json"
    config.write_text(
        json.dumps({"storage": db.config.model_dump(mode="json")}),
    )
    archive = tmp_path / "storage.jsonl"
    records = Records(db, "tenant", 1)
    await records.put("jobs", "registry", {"jobs": ["saved"]})
    runner = CliRunner()
    base = ["--config", str(config)]
    missing = await asyncio.to_thread(
        runner.invoke,
        storage_group,
        [*base, "backup", str(archive)],
    )
    assert missing.exit_code != 0
    assert not archive.exists()
    result = await asyncio.to_thread(
        runner.invoke,
        storage_group,
        [*base, "backup", str(archive), "--offline"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["counts"]["records"] == 1
    # Simulate lost records only inside this test's isolated database schema.
    async with db.transaction(write=True) as tx:
        await tx.execute(f"DELETE FROM {db.table('records')}")
    result = await asyncio.to_thread(
        runner.invoke,
        storage_group,
        [*base, "restore", str(archive), "--offline"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["restored"] is True
    assert (await records.get("jobs", "registry"))[0] == {"jobs": ["saved"]}


def test_cli_does_not_back_up_inactive_new_sqlite_schema(tmp_path):
    config = tmp_path / "local.json"
    original = json.dumps({"storage": {"deployment_id": "test"}})
    config.write_text(original)
    archive = tmp_path / "backup.jsonl"
    result = CliRunner().invoke(
        storage_group,
        [
            "--config",
            str(config),
            "backup",
            str(archive),
            "--offline",
        ],
    )
    assert result.exit_code != 0
    assert "require PostgreSQL" in result.output
    assert not archive.exists()
    assert config.read_text() == original
