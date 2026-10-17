# -*- coding: utf-8 -*-
"""Explicit initialization and inspection of configured SQL storage."""

import asyncio
import json
from pathlib import Path

import click

from ..config.utils import load_config
from ..constant import WORKING_DIR
from ..storage.errors import StorageError
from ..storage.factory import create_database
from ..storage.factory import create_storage
from ..storage.config import StorageBackend
from ..storage.migration.workspace import import_workspace
from ..storage.schema import initialize, inspect_storage


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


async def _import_workspace(config, root, manifest: Path) -> str:
    if config.backend != StorageBackend.POSTGRESQL:
        raise click.ClickException("Workspace import requires PostgreSQL")
    async with create_storage(config, root) as runtime:
        return await import_workspace(
            runtime.database,
            manifest,
            tenant_id=config.deployment_id,
            epoch=runtime.epoch,
        )


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
    config, root = ctx.obj
    try:
        store_id = asyncio.run(_import_workspace(config, root, manifest))
    except (StorageError, click.ClickException) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(
            "Import failed; check the manifest, source files and database "
            f"connection ({type(exc).__name__}).",
        ) from exc
    click.echo(json.dumps({"store_id": store_id, "imported": True}))
