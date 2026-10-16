# -*- coding: utf-8 -*-
"""Disposable SQLite query views of authoritative SQL history."""

from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory

from ....utils.io_utils import run_sync_io
from ...repositories.history import COLUMNS, SqlHistoryStore
from .legacy_history import SQLiteHistoryStore


class _SnapshotWriter(SQLiteHistoryStore):
    """Preserve source sequences and rebuild the derived search index."""

    def insert_page(self, rows: list[dict]) -> None:
        columns = ("seq", *COLUMNS)
        with self._conn:
            self._conn.executemany(
                f"INSERT INTO conversation_history ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [tuple(row[column] for column in columns) for row in rows],
            )

    def finish(self) -> None:
        if self._fts:
            with self._conn:
                self._rebuild_fts()
        self.close()


async def refresh_history_snapshot(
    history: SqlHistoryStore,
    destination: Path,
) -> None:
    """Publish only a complete snapshot; callers serialize readers/refreshes.

    All temporary files reside beside the destination, allowing atomic
    replacement. A failed refresh raises instead of serving stale history.
    """
    temporary = await run_sync_io(
        TemporaryDirectory,
        prefix=".snapshot-",
        dir=destination.parent,
    )
    writer = None
    try:
        path = Path(temporary.name) / "history.db"
        writer = await run_sync_io(_SnapshotWriter, path)
        async with aclosing(history.snapshot_pages()) as pages:
            async for rows in pages:
                await run_sync_io(writer.insert_page, rows)
        await run_sync_io(writer.finish)
        await run_sync_io(path.replace, destination)
    finally:
        if writer is not None:
            await run_sync_io(writer.close)
        await run_sync_io(temporary.cleanup)
