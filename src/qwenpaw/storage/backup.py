# -*- coding: utf-8 -*-
"""Offline, streaming backups of the fixed application SQL schema."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..utils.io_utils import run_sync_io
from .contracts.database import Database
from .errors import StorageConflictError, StorageIdentityError
from .repositories.history import COLUMNS
from .repositories.records import encode
from .schema import FORMAT_VERSION, identity, fence_write

# Identifiers come only from this fixed schema, never from archive input.
_COLUMNS = {
    "stores": (
        "store_id",
        "tenant_id",
        "workspace_id",
        "agent_id",
        "generation",
        "next_seq",
    ),
    "history": ("store_id", "seq", *COLUMNS),
    "checkpoints": (
        "store_id",
        "session_id",
        "generation",
        "revision",
        "payload",
    ),
    "records": ("tenant_id", "collection", "record_id", "revision", "payload"),
    "usage": ("tenant_id", "event_id", "payload"),
    "leases": ("store_id", "session_id", "owner_id", "token", "expires_at"),
    "migration_jobs": ("job_id", "phase", "payload"),
}
_KEYS = {
    "stores": ("store_id",),
    "history": ("store_id", "seq"),
    "checkpoints": ("store_id", "session_id"),
    "records": ("tenant_id", "collection", "record_id"),
    "usage": ("tenant_id", "event_id"),
    "leases": ("store_id", "session_id"),
    "migration_jobs": ("job_id",),
}
_METADATA = (
    "singleton",
    "deployment_id",
    "dataset_id",
    "format_version",
    "epoch",
    "revision",
    "status",
    "migration_id",
)
_BATCH_SIZE = 500


async def _require_expired_leases(db, tx) -> None:
    if await tx.one(
        f"SELECT 1 FROM {db.table('leases')} "
        f"WHERE expires_at > {db.clock()} LIMIT 1",
    ):
        raise StorageConflictError(
            "Stop workers and wait for leases to expire",
        )


def _write_lines(stream, digest, values) -> None:
    for value in values:
        line = (encode(value) + "\n").encode("utf-8")
        stream.write(line)
        digest.update(line)


def _finish_backup(stream, digest, counts):
    stream.write(
        (
            encode({"counts": counts, "sha256": digest.hexdigest()}) + "\n"
        ).encode("utf-8"),
    )
    stream.flush()
    os.fsync(stream.fileno())
    stream.close()


async def backup_storage(db: Database, destination: Path) -> dict:
    """Read all owned business tables in one consistent snapshot."""
    if destination.exists():
        raise StorageConflictError("Backup destination already exists")
    counts = dict.fromkeys(_COLUMNS, 0)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            async with db.transaction(snapshot=True) as tx:
                meta = await identity(db, tx)
                if (
                    meta["deployment_id"] != db.config.deployment_id
                    or meta["format_version"] != FORMAT_VERSION
                    or meta["status"] != "active"
                ):
                    raise StorageIdentityError(
                        "Unsupported or inactive backup identity",
                    )
                await _require_expired_leases(db, tx)
                await run_sync_io(
                    _write_lines,
                    stream,
                    digest,
                    [
                        {
                            "format": "qwenpaw-storage",
                            "version": 2,
                            "metadata": meta,
                        },
                    ],
                )
                for table, keys in _KEYS.items():
                    last = None
                    while True:
                        where = ""
                        params = ()
                        if last is not None:
                            where = (
                                f" WHERE ({', '.join(keys)}) > "
                                f"({db.binds(len(keys))})"
                            )
                            params = last
                        rows = await tx.fetch(
                            f"SELECT * FROM {db.table(table)}{where} "
                            f"ORDER BY {', '.join(keys)} LIMIT {_BATCH_SIZE}",
                            *params,
                        )
                        if not rows:
                            break
                        await run_sync_io(
                            _write_lines,
                            stream,
                            digest,
                            ({"table": table, "row": row} for row in rows),
                        )
                        counts[table] += len(rows)
                        last = tuple(rows[-1][key] for key in keys)
            await run_sync_io(_finish_backup, stream, digest, counts)
            # Same-filesystem link publishes atomically without overwriting.
            # No await follows publication, so cancellation cannot race it.
            os.link(temporary, destination)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)
    return counts


def _read_header(stream, digest, deployment_id: str) -> dict:
    line = stream.readline()
    header = json.loads(line)
    if header.get("format") != "qwenpaw-storage" or header.get("version") != 2:
        raise StorageIdentityError("Unsupported storage backup format")
    meta = header.get("metadata", {})
    if set(meta) != set(_METADATA):
        raise StorageIdentityError("Invalid backup metadata")
    if meta["deployment_id"] != deployment_id:
        raise StorageIdentityError("Backup belongs to another deployment")
    if type(meta["epoch"]) is not int or meta["epoch"] < 1:
        raise StorageIdentityError("Invalid backup epoch")
    if (
        meta["format_version"] != FORMAT_VERSION
        or meta["status"] != "active"
        or meta["singleton"] != 1
        or not meta["dataset_id"]
    ):
        raise StorageIdentityError("Unsupported or inactive backup identity")
    digest.update(line)
    return meta


def _read_batch(stream, digest):
    rows = []
    for _ in range(_BATCH_SIZE):
        line = stream.readline()
        if not line:
            raise StorageIdentityError("Backup is truncated")
        value = json.loads(line)
        if "table" not in value:
            if stream.read(1):
                raise StorageIdentityError("Unexpected trailing backup data")
            return rows, value
        table = value["table"]
        row = value.get("row", {})
        if table not in _COLUMNS or set(row) != set(_COLUMNS[table]):
            raise StorageIdentityError("Unknown table or columns in backup")
        rows.append((table, row))
        digest.update(line)
    return rows, None


async def restore_storage(db: Database, source: Path, *, epoch: int) -> dict:
    """Restore into empty storage, rolling back all rows on failure."""
    counts = dict.fromkeys(_COLUMNS, 0)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        meta = await run_sync_io(
            _read_header,
            stream,
            digest,
            db.config.deployment_id,
        )
        async with db.transaction(write=True) as tx:
            await fence_write(db, tx, epoch)
            for table in _COLUMNS:
                if (
                    await tx.one(f"SELECT 1 FROM {db.table(table)} LIMIT 1")
                    is not None
                ):
                    raise StorageConflictError(
                        "Restore requires empty target storage",
                    )
            footer = None
            while footer is None:
                rows, footer = await run_sync_io(_read_batch, stream, digest)
                for table, columns in _COLUMNS.items():
                    values = [
                        tuple(row[column] for column in columns)
                        for name, row in rows
                        if name == table
                    ]
                    if values:
                        await tx.many(
                            f"INSERT INTO {db.table(table)} "
                            f"({', '.join(columns)}) "
                            f"VALUES ({db.binds(len(columns))})",
                            values,
                        )
                        counts[table] += len(values)
            if footer != {"counts": counts, "sha256": digest.hexdigest()}:
                raise StorageIdentityError(
                    "Backup checksum or row counts do not match",
                )
            await _require_expired_leases(db, tx)
            assignments = ", ".join(
                f"{column}={db.bind(i)}"
                for i, column in enumerate(_METADATA, 1)
            )
            await tx.execute(
                f"UPDATE {db.table('metadata')} SET {assignments} "
                "WHERE singleton=1",
                *(meta[column] for column in _METADATA),
            )
    return counts
