# -*- coding: utf-8 -*-
"""Atomic, retry-safe token usage batches and daily aggregates."""

import json

from ..contracts.database import Database
from ..errors import StorageConflictError
from ..schema import fence_write
from .records import encode


COUNTERS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_eligible_input_tokens",
    "cache_observed_calls",
    "call_count",
)


def merge_daily_usage(totals: dict, delta: dict) -> None:
    """Merge counters while retaining provider, model and agent identity."""
    for key, incoming in delta.items():
        current = totals.setdefault(
            key,
            {**incoming, **dict.fromkeys(COUNTERS, 0)},
        )
        for counter in COUNTERS:
            current[counter] = current.get(counter, 0) + incoming.get(
                counter,
                0,
            )


class UsageRepository:
    """Commit each batch once, updating only the affected daily records."""

    def __init__(self, db: Database, tenant_id: str, epoch: int) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.epoch = epoch

    async def append_batch(self, batch_id: str, delta: dict) -> None:
        """Keep deduplication and daily totals in the same transaction."""
        db = self.db
        payload = encode(delta)
        async with db.transaction(write=True) as tx:
            await fence_write(db, tx, self.epoch)
            inserted = await tx.one(
                f"INSERT INTO {db.table('usage')} VALUES ({db.binds(3)}) "
                f"ON CONFLICT (tenant_id, event_id) DO NOTHING "
                f"RETURNING event_id",
                self.tenant_id,
                batch_id,
                payload,
            )
            if inserted is None:
                existing = await tx.one(
                    f"SELECT payload FROM {db.table('usage')} "
                    f"WHERE tenant_id={db.bind(1)} AND event_id={db.bind(2)}",
                    self.tenant_id,
                    batch_id,
                )
                if existing is None or existing["payload"] != payload:
                    raise StorageConflictError("Usage batch identity reused")
                return
            for day in sorted(delta):
                # Establish a row before taking its write lock. Concurrent
                # batches for the same day then merge into the latest payload.
                await tx.execute(
                    f"INSERT INTO {db.table('records')} "
                    f"VALUES ({db.binds(3)}, 1, {db.bind(4)}) "
                    f"ON CONFLICT (tenant_id, collection, record_id) "
                    f"DO NOTHING",
                    self.tenant_id,
                    "token_usage",
                    day,
                    "{}",
                )
                row = await tx.one(
                    f"UPDATE {db.table('records')} SET revision=revision+1 "
                    f"WHERE tenant_id={db.bind(1)} "
                    f"AND collection={db.bind(2)} AND record_id={db.bind(3)} "
                    f"RETURNING payload",
                    self.tenant_id,
                    "token_usage",
                    day,
                )
                if row is None:
                    raise StorageConflictError(
                        "Usage aggregate row disappeared",
                    )
                totals = json.loads(row["payload"])
                merge_daily_usage(totals, delta[day])
                await tx.execute(
                    f"UPDATE {db.table('records')} SET payload={db.bind(1)} "
                    f"WHERE tenant_id={db.bind(2)} "
                    f"AND collection={db.bind(3)} AND record_id={db.bind(4)}",
                    encode(totals),
                    self.tenant_id,
                    "token_usage",
                    day,
                )

    async def load(self, start_date: str, end_date: str) -> dict:
        """Read only daily aggregates in the requested ISO date range."""
        db = self.db
        async with db.transaction() as tx:
            rows = await tx.fetch(
                f"SELECT record_id, payload FROM {db.table('records')} "
                f"WHERE tenant_id={db.bind(1)} AND collection={db.bind(2)} "
                f"AND record_id>={db.bind(3)} AND record_id<={db.bind(4)} "
                f"ORDER BY record_id",
                self.tenant_id,
                "token_usage",
                start_date,
                end_date,
            )
        return {row["record_id"]: json.loads(row["payload"]) for row in rows}
