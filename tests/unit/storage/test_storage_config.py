# -*- coding: utf-8 -*-
"""Root configuration preserves backend selection without storing secrets."""

import json

from qwenpaw.config.config import Config
from qwenpaw.config.utils import load_config, save_config
from qwenpaw.storage.config import StorageBackend


def test_existing_config_defaults_to_sqlite():
    assert Config.model_validate({}).storage.backend == StorageBackend.SQLITE


def test_postgres_config_survives_save_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TEST_STORAGE_DSN",
        "postgresql://user:secret@localhost/db",
    )
    config = Config.model_validate(
        {
            "storage": {
                "backend": "postgresql",
                "deployment_id": "test-deployment",
                "postgresql": {
                    "schema": "custom",
                    "dsn_env": "TEST_STORAGE_DSN",
                },
            },
        },
    )
    path = tmp_path / "config.json"
    save_config(config, path)
    restored = load_config(path)
    assert restored.storage == config.storage
    assert Config.model_validate(config.model_dump()).storage == config.storage
    payload = path.read_text(encoding="utf-8")
    assert "postgresql://user:secret@localhost/db" not in payload
    assert json.loads(payload)["storage"]["postgresql"]["schema"] == "custom"
