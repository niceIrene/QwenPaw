# -*- coding: utf-8 -*-
"""Import legacy application-wide usage totals without merging duplicates."""

import asyncio
from datetime import date
from pathlib import Path

from ..contracts.database import Database
from ..errors import StorageConflictError, StorageIdentityError
from ..repositories.records import encode
from ..repositories.usage import COUNTERS
from ..schema import fence_write
from .legacy import _read_checkpoint


def _read_usage(path: Path) -> dict:
    data = _read_checkpoint(path)
    for day, entries in data.items():
        if date.fromisoformat(day).isoformat() != day or not isinstance(
            entries,
            dict,
        ):
            raise StorageIdentityError("Invalid usage date or daily records")
        for entry in entries.values():
            if not isinstance(entry, dict):
                raise StorageIdentityError("Invalid usage row")
            for counter in COUNTERS:
                value = entry.get(counter, 0)
                if type(value) is not int or value < 0:
                    raise StorageIdentityError(
                        "Usage counters must be non-negative integers",
                    )
            for field in ("provider_id", "model_name", "agent_id"):
                if (
                    field in entry
                    and entry[field] is not None
                    and not isinstance(entry[field], str)
                ):
                    raise StorageIdentityError("Invalid usage identity")
    # Retain legacy keys and missing agent_id: the query API distinguishes
    # historical untracked agents from explicitly empty agent identities.
    return data


async def import_usage(
    db: Database,
    source: Path,
    *,
    tenant_id: str,
    epoch: int,
) -> int:
    """Import once into empty tenant usage totals, with all workers stopped."""
    data = await asyncio.to_thread(_read_usage, source)
    async with db.transaction(write=True) as tx:
        await fence_write(db, tx, epoch)
        existing = await tx.one(
            f"SELECT record_id FROM {db.table('records')} "
            f"WHERE tenant_id={db.bind(1)} "
            f"AND collection={db.bind(2)} LIMIT 1",
            tenant_id,
            "token_usage",
        )
        if existing is not None:
            raise StorageConflictError("Target usage totals already exist")
        marker = await tx.one(
            f"INSERT INTO {db.table('usage')} VALUES ({db.binds(3)}) "
            f"ON CONFLICT (tenant_id, event_id) DO NOTHING RETURNING event_id",
            tenant_id,
            "legacy-token-usage-import",
            encode({"days": len(data)}),
        )
        if marker is None:
            raise StorageConflictError("Legacy usage was already imported")
        for day, entries in data.items():
            inserted = await tx.one(
                f"INSERT INTO {db.table('records')} "
                f"VALUES ({db.binds(3)}, 1, {db.bind(4)}) "
                f"ON CONFLICT (tenant_id, collection, record_id) "
                f"DO NOTHING RETURNING revision",
                tenant_id,
                "token_usage",
                day,
                encode(entries),
            )
            if inserted is None:
                raise StorageConflictError(
                    "Target usage totals changed during import",
                )
    return len(data)
