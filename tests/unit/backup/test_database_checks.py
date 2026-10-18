# -*- coding: utf-8 -*-
"""File-only backup must not silently stand in for a PostgreSQL backup."""

import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qwenpaw.backup._ops import create, restore
from qwenpaw.backup._utils.constants import PREFIX_CONFIG
from qwenpaw.backup.models import (
    BackupMeta,
    BackupValidationError,
    RestoreBackupRequest,
)
from qwenpaw.config.config import Config


def test_pg_workspace_backup_fails_without_publishing_archive(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "backup.zip"
    config = Config(storage={"backend": "postgresql", "deployment_id": "test"})
    monkeypatch.setattr(create, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(create, "zip_path", lambda _: destination)
    monkeypatch.setattr(create, "load_config", lambda: config)
    collect = Mock()
    monkeypatch.setattr(create, "add_files_to_zip", collect)
    with pytest.raises(BackupValidationError, match="PostgreSQL"):
        create.create_backup(
            BackupMeta(name="test"),
            [],
            lambda _: None,
            threading.Event(),
        )
    collect.assert_not_called()
    assert not destination.exists()
    assert not destination.with_suffix(".tmp").exists()


@pytest.mark.parametrize("current_pg", [False, True])
def test_pg_restore_rejected_in_preflight_and_before_mutation(
    tmp_path,
    monkeypatch,
    current_pg,
):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("meta.json", BackupMeta(name="old").model_dump_json())
        zf.writestr(
            PREFIX_CONFIG,
            json.dumps(
                {
                    "storage": {
                        "backend": "sqlite" if current_pg else "postgresql",
                    },
                },
            ),
        )
    config = Config(
        storage={"backend": "postgresql" if current_pg else "sqlite"},
    )
    monkeypatch.setattr(restore, "load_config", lambda: config)
    monkeypatch.setattr(restore, "find_zip_path", lambda _: archive)
    monkeypatch.setattr(
        restore,
        "resolve_signature_action",
        lambda *a, **k: "sign_trusted",
    )
    sign = Mock()
    monkeypatch.setattr(restore, "sign_trusted_backup", sign)
    for operation in (
        restore.preflight_restore,
        restore._restore_sync_locked,  # pylint: disable=protected-access
    ):
        with pytest.raises(BackupValidationError, match="PostgreSQL"):
            operation("old", RestoreBackupRequest())
    sign.assert_not_called()


def test_non_workspace_archive_remains_available_with_pg(tmp_path):
    from qwenpaw.backup._ops.database_checks import (
        check_backup_database,
        check_restore_database,
    )

    config = SimpleNamespace(storage=SimpleNamespace(backend="postgresql"))
    check_backup_database(config, include_agents=False)
    archive = tmp_path / "skills.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        check_restore_database(
            zf,
            RestoreBackupRequest(
                include_agents=False,
                include_global_config=False,
            ),
            config,
        )


def test_config_only_pg_restore_keeps_same_database_identity(tmp_path):
    from qwenpaw.backup._ops.database_checks import check_restore_database

    config = Config(storage={"backend": "postgresql", "deployment_id": "same"})
    with zipfile.ZipFile(tmp_path / "config.zip", "w") as zf:
        zf.writestr(
            PREFIX_CONFIG,
            json.dumps({"storage": config.storage.model_dump(mode="json")}),
        )
        check_restore_database(
            zf,
            RestoreBackupRequest(
                include_agents=False,
                include_global_config=True,
            ),
            config,
        )
