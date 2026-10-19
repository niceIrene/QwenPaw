# -*- coding: utf-8 -*-
"""Disposable SQLite query views of authoritative SQL history."""

from contextlib import aclosing
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from ....utils.io_utils import run_sync_io
from ...repositories.history import COLUMNS, SqlHistoryStore
from .history_schema import (
    create_history_tables,
    create_history_fts,
    rebuild_history_fts,
)


class _SnapshotWriter:
    """Build a new disposable query database without legacy recovery state."""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._conn:
                create_history_tables(self._conn)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def insert_page(self, rows: list[dict]) -> None:
        columns = ("seq", *COLUMNS)
        with self._conn:
            self._conn.executemany(
                f"INSERT INTO conversation_history ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [tuple(row[column] for column in columns) for row in rows],
            )

    def finish(self, *, search_index: bool) -> None:
        try:
            if search_index:
                with self._conn:
                    create_history_fts(self._conn)
                    rebuild_history_fts(self._conn)
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc).lower():
                raise
        finally:
            self.close()


async def refresh_history_snapshot(
    history: SqlHistoryStore,
    destination: Path,
    *,
    search_index: bool = True,
    **selection,
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
        async with aclosing(history.snapshot_pages(**selection)) as pages:
            async for rows in pages:
                await run_sync_io(writer.insert_page, rows)
        await run_sync_io(writer.finish, search_index=search_index)
        await run_sync_io(path.replace, destination)
    finally:
        if writer is not None:
            await run_sync_io(writer.close)
        await run_sync_io(temporary.cleanup)
