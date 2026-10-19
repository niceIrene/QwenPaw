# -*- coding: utf-8 -*-
"""Offline import of cron definitions and explicitly mapped history files."""

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ...app.crons.models import CronExecutionRecord, JobsFile
from ..contracts.database import Database
from ..errors import StorageConflictError, StorageIdentityError
from ..repositories.records import encode
from ..schema import fence_write
from .legacy import _read_checkpoint


class CronImport(BaseModel):
    """Paths are relative to the manifest; history keys are job IDs."""

    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    jobs: Path
    history: dict[str, Path] = Field(default_factory=dict)


def _read_cron_payloads(manifest_path: Path) -> tuple[CronImport, dict, dict]:
    manifest = CronImport.model_validate(_read_checkpoint(manifest_path))
    root = manifest_path.resolve().parent
    raw = _read_checkpoint(root / manifest.jobs)
    # Match JsonJobRepository's historical default, without creating missing
    # files or running startup migrations against the source workspace.
    for job in raw.get("jobs", []):
        if isinstance(job, dict):
            runtime = job.setdefault("runtime", {})
            if isinstance(runtime, dict):
                runtime.setdefault("share_session", True)
    jobs = JobsFile.model_validate(raw)
    ids = [job.id for job in jobs.jobs]
    if any(not job_id for job_id in ids) or len(set(ids)) != len(ids):
        raise StorageIdentityError("Cron jobs require unique non-empty IDs")
    history = {}
    adapter = TypeAdapter(list[CronExecutionRecord])
    for job_id, path in manifest.history.items():
        if job_id not in ids:
            raise StorageIdentityError("History refers to an unknown cron job")
        with (root / path).open(encoding="utf-8") as stream:
            records = adapter.validate_python(json.load(stream))
        history[job_id] = [
            record.model_dump(mode="json") for record in records
        ]
    return manifest, jobs.model_dump(mode="json"), history


async def import_cron(
    db: Database,
    manifest_path: Path,
    *,
    tenant_id: str,
    epoch: int,
) -> int:
    """Import into empty cron records, including an existing session scope."""
    manifest, jobs, history = await asyncio.to_thread(
        _read_cron_payloads,
        manifest_path,
    )
    key = encode([manifest.workspace_id, manifest.agent_id])
    async with db.transaction(write=True) as tx:
        await fence_write(db, tx, epoch)
        for collection, payload in (("jobs", jobs), ("job_history", history)):
            inserted = await tx.one(
                f"INSERT INTO {db.table('records')} "
                f"VALUES ({db.binds(3)}, 1, {db.bind(4)}) "
                f"ON CONFLICT (tenant_id, collection, record_id) "
                f"DO NOTHING RETURNING revision",
                tenant_id,
                collection,
                key,
                encode(payload),
            )
            if inserted is None:
                raise StorageConflictError("Target cron data already exists")
    return len(jobs["jobs"])
