# -*- coding: utf-8 -*-
"""Doctor must inspect active PG data rather than abandoned JSON files."""

import asyncio

import pytest

from qwenpaw.cli.doctor_checks import check_cron_jobs_files
from qwenpaw.config.config import AgentProfileRef, Config
from qwenpaw.storage.repositories.records import Records, encode


async def test_doctor_validates_pg_cron_without_mutation(
    db,
    tmp_path,
    monkeypatch,
):
    if db.config.backend != "postgresql":
        pytest.skip("Default file diagnostics have their own regression tests")
    (tmp_path / "jobs.json").write_text("invalid abandoned file")
    cfg = Config(storage=db.config)
    cfg.agents.profiles = {
        "a": AgentProfileRef(id="a", workspace_dir=str(tmp_path)),
    }
    records = Records(db, db.config.deployment_id, 1)
    key = encode(["a", "a"])
    ok, detail = await asyncio.to_thread(check_cron_jobs_files, cfg)
    assert ok and "a: 0 job(s)" in detail
    assert await records.get("jobs", key) is None

    payload = {"jobs": "private-invalid-payload"}
    await records.put("jobs", key, payload)
    ok, detail = await asyncio.to_thread(check_cron_jobs_files, cfg)
    assert not ok and "ValidationError" in detail
    assert "private-invalid-payload" not in detail
    assert await records.get("jobs", key) == (payload, 1)

    await records.put("jobs", key, {"jobs": []}, revision=1)
    ok, detail = await asyncio.to_thread(check_cron_jobs_files, cfg)
    assert ok and "PostgreSQL" in detail and "a: 0 job(s)" in detail
    assert await records.get("jobs", key) == ({"jobs": []}, 2)

    monkeypatch.delenv(db.config.postgresql.dsn_env)
    ok, detail = await asyncio.to_thread(check_cron_jobs_files, cfg)
    assert not ok and "PostgreSQL cron check failed" in detail
