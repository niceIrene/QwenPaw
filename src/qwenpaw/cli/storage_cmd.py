# -*- coding: utf-8 -*-
"""Explicit initialization and inspection of configured SQL storage."""

import asyncio
import json
from pathlib import Path

import click

from ..config.utils import load_config
from ..constant import WORKING_DIR
from ..storage.errors import StorageError
from ..storage.backup import backup_storage, restore_storage
from ..storage.factory import create_database
from ..storage.factory import create_storage
from ..storage.config import StorageBackend
from ..storage.migration.workspace import import_workspace
from ..storage.migration.cron import import_cron
from ..storage.migration.usage import import_usage
from ..storage.schema import initialize
from ..storage.migration.service import inspect as inspect_storage


@click.group("storage")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, exists=True),
    help="Root config.json; relative SQLite paths use its parent directory.",
)
@click.pass_context
def storage_group(ctx: click.Context, config_path: Path | None) -> None:
    """Initialize or inspect SQL storage without switching backends."""
    config = load_config(config_path).storage
    root = config_path.resolve().parent if config_path else WORKING_DIR
    ctx.obj = (config, root)


async def _inspect_storage(config, root, *, create: bool) -> dict:
    if not config.deployment_id:
        raise click.ClickException("Set storage.deployment_id in config.json")
    database = create_database(config, root)
    try:
        await database.open(create=create)
        if create:
            await initialize(database)
        return {
            "backend": config.backend.value,
            **await inspect_storage(database),
        }
    finally:
        await database.close()


def _run(ctx: click.Context, *, create: bool) -> None:
    config, root = ctx.obj
    try:
        result = asyncio.run(_inspect_storage(config, root, create=create))
    except (StorageError, click.ClickException) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        # Driver messages can contain connection details. Do not print them.
        raise click.ClickException(
            "Cannot access configured storage; check credentials, "
            "connectivity and namespace permissions "
            f"({type(exc).__name__}).",
        ) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@storage_group.command("init")
@click.pass_context
def init_storage(ctx: click.Context) -> None:
    """Initialize an empty namespace or validate existing owned tables."""
    _run(ctx, create=True)


@storage_group.command("check")
@click.pass_context
def check_storage(ctx: click.Context) -> None:
    """Check connectivity, identity and counts without initializing a store."""
    _run(ctx, create=False)


async def _import_data(config, root, source: Path, importer):
    if config.backend != StorageBackend.POSTGRESQL:
        raise click.ClickException("Legacy import requires PostgreSQL")
    async with create_storage(config, root) as runtime:
        options = {}
        report = []
        if importer is import_workspace:
            options["reference_report"] = report
        result = await importer(
            runtime.database,
            source,
            tenant_id=config.deployment_id,
            epoch=runtime.epoch,
            **options,
        )
        if importer is import_workspace:
            return {
                "store_id": result,
                "imported": True,
                "requires_review": any(r["requires_review"] for r in report),
                "history_references": report,
            }
        return result


@storage_group.command("import-workspace")
@click.argument("manifest", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--offline",
    is_flag=True,
    required=True,
    help="Confirm all application workers are stopped before importing.",
)
@click.pass_context
def import_workspace_command(
    ctx: click.Context,
    manifest: Path,
    offline: bool,
) -> None:
    """Import legacy sessions, history and chats into an unused PG scope."""
    if not offline:
        raise click.ClickException("Stop all workers and pass --offline")
    result = _run_import(ctx, manifest, import_workspace)
    click.echo(json.dumps(result))


def _run_import(ctx: click.Context, source: Path, importer):
    config, root = ctx.obj
    try:
        return asyncio.run(_import_data(config, root, source, importer))
    except (StorageError, click.ClickException) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(
            "Import failed; check the manifest, source files and database "
            f"connection ({type(exc).__name__}).",
        ) from exc


@storage_group.command("import-cron")
@click.argument("manifest", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--offline",
    is_flag=True,
    required=True,
    help="Confirm all application workers are stopped.",
)
@click.pass_context
def import_cron_command(
    ctx: click.Context,
    manifest: Path,
    offline: bool,
) -> None:
    """Import cron definitions and history into empty cron records."""
    if not offline:
        raise click.ClickException("Stop all workers and pass --offline")
    count = _run_import(ctx, manifest, import_cron)
    click.echo(json.dumps({"jobs": count, "imported": True}))


@storage_group.command("import-usage")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--offline",
    is_flag=True,
    required=True,
    help="Confirm all application workers are stopped.",
)
@click.pass_context
def import_usage_command(
    ctx: click.Context,
    source: Path,
    offline: bool,
) -> None:
    """Import application-wide legacy usage JSON into empty PG totals."""
    if not offline:
        raise click.ClickException("Stop all workers and pass --offline")
    count = _run_import(ctx, source, import_usage)
    click.echo(json.dumps({"days": count, "imported": True}))


async def _backup_operation(config, root, path: Path, *, restore: bool):
    if config.backend != StorageBackend.POSTGRESQL:
        raise click.ClickException("These backup commands require PostgreSQL")
    async with create_storage(config, root) as runtime:
        if restore:
            return await restore_storage(
                runtime.database,
                path,
                epoch=runtime.epoch,
            )
        return await backup_storage(runtime.database, path)


def _run_backup(ctx: click.Context, path: Path, *, restore: bool) -> None:
    config, root = ctx.obj
    try:
        counts = asyncio.run(
            _backup_operation(config, root, path, restore=restore),
        )
    except (StorageError, click.ClickException) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(
            "Storage backup/restore failed; check the file and database "
            f"connection ({type(exc).__name__}).",
        ) from exc
    click.echo(
        json.dumps(
            {"restored" if restore else "backed_up": True, "counts": counts},
        ),
    )


@storage_group.command("backup")
@click.argument("destination", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--offline",
    is_flag=True,
    required=True,
    help="Confirm all application workers are stopped.",
)
@click.pass_context
def backup_command(
    ctx: click.Context,
    destination: Path,
    offline: bool,
) -> None:
    """Back up all application SQL tables; workspace files remain separate."""
    if not offline:
        raise click.ClickException("Stop all workers and pass --offline")
    _run_backup(ctx, destination, restore=False)


@storage_group.command("restore")
@click.argument(
    "source",
    type=click.Path(exists=True, path_type=Path, dir_okay=False),
)
@click.option(
    "--offline",
    is_flag=True,
    required=True,
    help="Confirm all application workers are stopped.",
)
@click.pass_context
def restore_command(ctx: click.Context, source: Path, offline: bool) -> None:
    """Restore SQL data into empty storage of the same deployment."""
    if not offline:
        raise click.ClickException("Stop all workers and pass --offline")
    _run_backup(ctx, source, restore=True)
