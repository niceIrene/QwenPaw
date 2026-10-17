# -*- coding: utf-8 -*-
"""Buffered PG usage writes with stable batch identities across retries."""

import asyncio
import logging
from uuid import uuid4

from ..storage.repositories.usage import UsageRepository, merge_daily_usage
from .buffer import _UsageEvent, _apply_event

logger = logging.getLogger(__name__)


class DatabaseTokenUsageBuffer:
    """Keep the synchronous producer API and serialize flushes and reads."""

    def __init__(self, repository: UsageRepository, flush_interval: int = 10):
        self.repository = repository
        self._flush_interval = flush_interval
        self._pending: dict = {}
        self._batch: tuple[str, dict] | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def enqueue(self, event: _UsageEvent) -> None:
        """Accumulate on the producer's event loop without database I/O."""
        _apply_event(self._pending, event)

    def take_pending_events(self) -> list[_UsageEvent]:
        """Allow backend rebinding only after a successful final flush."""
        if self._task is not None or self._batch is not None or self._pending:
            raise RuntimeError("Stop and flush token usage before rebinding")
        return []

    def start(self) -> None:
        """Start the periodic writer once."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._flush_loop(),
                name="token-usage-database-flush",
            )

    async def stop(self) -> None:
        """Cancel the timer and persist the final batch before pool closure."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()

    async def _flush_locked(self) -> None:
        # A failed or cancelled write retains the exact ID and payload.
        # Producers append to a separate dictionary while the DB is awaited.
        if self._batch is not None:
            await self.repository.append_batch(*self._batch)
            self._batch = None
        if self._pending:
            self._batch = (f"token-usage:{uuid4()}", self._pending)
            self._pending = {}
            await self.repository.append_batch(*self._batch)
            self._batch = None

    async def flush(self) -> None:
        """Flush without discarding events on failure or cancellation."""
        async with self._lock:
            await self._flush_locked()

    async def get_merged_data(
        self,
        start_date: str = "0001-01-01",
        end_date: str = "9999-12-31",
    ) -> dict:
        """Read durable totals and include events arriving during the read."""
        async with self._lock:
            await self._flush_locked()
            result = await self.repository.load(start_date, end_date)
            # No other flush can run while reading. Pending events therefore
            # cannot already be part of this database result.
            for day, entries in self._pending.items():
                if not start_date <= day <= end_date:
                    continue
                merge_daily_usage(result.setdefault(day, {}), entries)
            return result

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception:
                logger.exception(
                    "token_usage: database flush failed; will retry",
                )
