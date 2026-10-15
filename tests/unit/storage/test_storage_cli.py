# -*- coding: utf-8 -*-
"""CLI initialization is explicit and inspection cannot create databases."""

import json

from click.testing import CliRunner

from qwenpaw.cli.storage_cmd import storage_group


def test_init_and_check_do_not_switch_or_overwrite_config(tmp_path):
    config_path = tmp_path / "config.json"
    original = json.dumps({"storage": {"deployment_id": "test"}})
    config_path.write_text(original)
    runner = CliRunner()
    args = ["--config", str(config_path)]
    missing = runner.invoke(storage_group, [*args, "check"])
    assert missing.exit_code != 0
    assert not (tmp_path / "state").exists()
    initialized = runner.invoke(storage_group, [*args, "init"])
    assert initialized.exit_code == 0, initialized.output
    identity = json.loads(initialized.output)["identity"]
    for command in ("init", "check"):
        result = runner.invoke(storage_group, [*args, command])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["identity"] == identity
    assert config_path.read_text() == original


def test_cli_does_not_print_driver_connection_secrets(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"storage": {"deployment_id": "test"}}))

    def fail(*_args):
        raise RuntimeError("postgresql://user:secret@host/db")

    monkeypatch.setattr("qwenpaw.cli.storage_cmd.create_database", fail)
    result = CliRunner().invoke(
        storage_group,
        ["--config", str(config_path), "check"],
    )
    assert result.exit_code != 0
    assert "secret" not in result.output
    assert "RuntimeError" in result.output
