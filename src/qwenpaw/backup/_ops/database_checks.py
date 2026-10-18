# -*- coding: utf-8 -*-
"""Reject filesystem operations that would omit authoritative PG state."""

import json
import zipfile

from .._utils.constants import PREFIX_CONFIG
from ..models import BackupValidationError, RestoreBackupRequest
from ...storage.config import StorageBackend, StorageConfig


def _uses_postgresql(config) -> bool:
    storage = getattr(config, "storage", None)
    return getattr(storage, "backend", None) == StorageBackend.POSTGRESQL


def check_backup_database(config, *, include_agents: bool) -> None:
    """Config/secrets/skills archives are supported; PG workspaces are not."""
    if include_agents and _uses_postgresql(config):
        raise BackupValidationError(
            "postgresql_backup_unsupported",
            "Workspace ZIP backups do not include PostgreSQL data. "
            "Back up PostgreSQL and workspace files together while stopped. "
            "Config, secrets and skills can be archived without workspaces.",
        )


def check_restore_database(
    archive: zipfile.ZipFile,
    request: RestoreBackupRequest,
    config,
) -> None:
    """Check both current storage and archived config before changing files."""
    if not (request.include_agents or request.include_global_config):
        return
    source_uses_pg = False
    storage_changes = False
    if PREFIX_CONFIG in archive.namelist():
        source = json.loads(archive.read(PREFIX_CONFIG))
        storage = StorageConfig.model_validate(source.get("storage", {}))
        source_uses_pg = storage.backend == StorageBackend.POSTGRESQL
        storage_changes = storage != getattr(
            config,
            "storage",
            StorageConfig(),
        )
    changes_data_or_storage = request.include_agents or (
        request.include_global_config and storage_changes
    )
    if changes_data_or_storage and (
        _uses_postgresql(config) or source_uses_pg
    ):
        raise BackupValidationError(
            "postgresql_restore_unsupported",
            "ZIP restore cannot restore workspace PostgreSQL state or "
            "switch its database configuration. "
            "Restore a matching PostgreSQL backup and filesystem snapshot "
            "while all application workers are stopped.",
        )
