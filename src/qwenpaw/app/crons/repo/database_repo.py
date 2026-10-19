# -*- coding: utf-8 -*-
"""Workspace cron definitions and bounded execution history in SQL records."""

import asyncio

from pydantic import PrivateAttr

from .base import BaseJobRepository
from ..models import CronExecutionRecord, JobsFile
from ....storage.errors import StorageConflictError
from ....storage.models import StorageScope
from ....storage.repositories.records import Records, encode


class _JobsSnapshot(JobsFile):
    """Revision belongs to the loaded document, not the repository instance."""

    _revision: int = PrivateAttr(default=0)
    _owner: object = PrivateAttr(default=None)


class DatabaseJobRepository(BaseJobRepository):
    """Keep existing job semantics with scoped, revision-checked writes."""

    # The private snapshot type belongs exclusively to this repository.
    # pylint: disable=protected-access

    def __init__(self, records: Records, scope: StorageScope) -> None:
        if records.tenant_id != scope.tenant_id:
            raise ValueError("Job registry tenant does not match its scope")
        self.records = records
        self.key = encode([scope.workspace_id, scope.agent_id])
        self.path = f"database:jobs:{self.key}"
        self._history_lock = asyncio.Lock()

    async def load(self) -> JobsFile:
        """Load job definitions with their exact compare-and-set revision."""
        record = await self.records.get("jobs", self.key)
        snapshot = _JobsSnapshot.model_validate(record[0] if record else {})
        snapshot._revision = record[1] if record else 0
        snapshot._owner = self
        return snapshot

    async def save(self, jobs_file: JobsFile) -> None:
        """Persist only a snapshot read from this repository."""
        if (
            not isinstance(jobs_file, _JobsSnapshot)
            or jobs_file._owner is not self
        ):
            raise StorageConflictError("Load the job registry before saving")
        jobs_file._revision = await self.records.put(
            "jobs",
            self.key,
            jobs_file.model_dump(mode="json"),
            revision=jobs_file._revision,
        )

    async def get_history(self, job_id: str) -> list[CronExecutionRecord]:
        """Read history independently of the live scheduler's cache."""
        record = await self.records.get("job_history", self.key)
        payload = record[0] if record else {}
        return [
            CronExecutionRecord.model_validate(item)
            for item in payload.get(job_id, [])
        ]

    async def append_history(
        self,
        job_id: str,
        record: CronExecutionRecord,
        *,
        limit: int = 50,
    ) -> list[CronExecutionRecord]:
        """Serialize local callbacks and reject cross-instance stale writes."""
        async with self._history_lock:
            current = await self.records.get("job_history", self.key)
            payload, revision = current if current else ({}, 0)
            records = [
                record.model_dump(mode="json"),
                *payload.get(job_id, []),
            ]
            del records[limit:]
            payload[job_id] = records
            await self.records.put(
                "job_history",
                self.key,
                payload,
                revision=revision,
            )
            return [
                CronExecutionRecord.model_validate(item) for item in records
            ]

    async def delete_history(self, job_id: str) -> None:
        """Delete a job's history while preserving other jobs' records."""
        async with self._history_lock:
            current = await self.records.get("job_history", self.key)
            if current is not None and job_id in current[0]:
                payload, revision = current
                del payload[job_id]
                await self.records.put(
                    "job_history",
                    self.key,
                    payload,
                    revision=revision,
                )

    async def prune_orphan_history(self, valid_job_ids: set[str]) -> None:
        """Prune histories for jobs removed from the persisted registry."""
        async with self._history_lock:
            current = await self.records.get("job_history", self.key)
            if current is None:
                return
            payload, revision = current
            retained = {
                key: value
                for key, value in payload.items()
                if key in valid_job_ids
            }
            if len(retained) != len(payload):
                await self.records.put(
                    "job_history",
                    self.key,
                    retained,
                    revision=revision,
                )
