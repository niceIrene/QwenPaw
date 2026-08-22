# -*- coding: utf-8 -*-
"""Tests for CodeActConfig, its migration, and apply_codeact_mode."""

import json
from threading import Lock

from qwenpaw.config import utils as config_utils
from qwenpaw.config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    AgentsConfig,
    CodeActConfig,
    Config,
    load_agent_config,
    migrate_general_harness_to_codeact,
)


def test_codeact_defaults_off():
    cfg = CodeActConfig()
    assert cfg.enabled is False
    assert cfg.tool_routing == "repl-only"
    assert cfg.identity_text is None


def test_agent_profile_defaults_codeact_off():
    profile = AgentProfileConfig(id="a", name="a")
    assert profile.codeact.enabled is False
    assert profile.codeact.tool_routing == "repl-only"
    # Deprecated field stays unset so saves never write it back.
    assert profile.general_harness is None


def test_agent_profile_round_trip_with_codeact():
    profile = AgentProfileConfig(
        id="a",
        name="a",
        codeact=CodeActConfig(
            enabled=True,
            tool_routing="hybrid",
            identity_text="# Role\n\ncustom identity",
        ),
    )
    restored = AgentProfileConfig.model_validate(profile.model_dump())
    assert restored.codeact.enabled is True
    assert restored.codeact.tool_routing == "hybrid"
    assert restored.codeact.identity_text == "# Role\n\ncustom identity"


def test_agent_profile_missing_codeact_field_backfills_default():
    """Older agent.json files without the field must load with defaults."""
    profile = AgentProfileConfig.model_validate({"id": "a", "name": "a"})
    assert profile.codeact.enabled is False


def test_save_payload_omits_deprecated_general_harness():
    profile = AgentProfileConfig(id="a", name="a")
    payload = profile.model_dump(exclude_none=True)
    assert "general_harness" not in payload


def test_migrate_enabled_harness_to_codeact():
    data = {
        "general_harness": {
            "enabled": True,
            "identity_text": "custom",
        },
    }

    assert migrate_general_harness_to_codeact(data) is True
    assert "general_harness" not in data
    assert data["codeact"] == {
        "enabled": True,
        "identity_text": "custom",
        "tool_routing": "repl-only",
    }


def test_migrate_disabled_harness_only_pops_key():
    data = {"general_harness": {"enabled": False}}

    assert migrate_general_harness_to_codeact(data) is True
    assert "general_harness" not in data
    assert "codeact" not in data


def test_migrate_existing_codeact_wins():
    data = {
        "general_harness": {"enabled": True, "identity_text": "old"},
        "codeact": {"enabled": True, "tool_routing": "hybrid"},
    }

    assert migrate_general_harness_to_codeact(data) is True
    assert data["codeact"]["tool_routing"] == "hybrid"
    assert "identity_text" not in data["codeact"]


def test_migrate_legacy_codeact_mode_string():
    data = {"codeact_mode": "required"}

    assert migrate_general_harness_to_codeact(data) is True
    assert "codeact_mode" not in data
    assert data["codeact"] == {"enabled": True, "tool_routing": "repl-only"}

    data = {"codeact_mode": "off"}
    assert migrate_general_harness_to_codeact(data) is True
    assert data["codeact"]["tool_routing"] == "off"


def test_migrate_noop_without_legacy_keys():
    data = {"id": "a", "codeact": {"enabled": True}}

    assert migrate_general_harness_to_codeact(data) is False
    assert data == {"id": "a", "codeact": {"enabled": True}}


def _wire_agent(tmp_path, monkeypatch, raw):
    workspace_dir = tmp_path / "workspaces" / "agent"
    workspace_dir.mkdir(parents=True)
    agent_config_path = workspace_dir / "agent.json"
    agent_config_path.write_text(json.dumps(raw), encoding="utf-8")

    root_config = Config(
        agents=AgentsConfig(
            active_agent="agent",
            profiles={
                "agent": AgentProfileRef(
                    id="agent",
                    workspace_dir=str(workspace_dir),
                ),
            },
        ),
    )
    monkeypatch.setattr(config_utils, "load_config", lambda: root_config)
    monkeypatch.setattr(config_utils, "_agent_config_cache", {})
    monkeypatch.setattr(config_utils, "_agent_config_lock", Lock())
    return agent_config_path


def test_loaded_agent_config_migration_persists(tmp_path, monkeypatch):
    raw = AgentProfileConfig(id="agent", name="Agent").model_dump(
        exclude_none=True,
    )
    # Simulate a pre-migration file: general_harness present, no codeact key.
    raw.pop("codeact", None)
    raw["general_harness"] = {"enabled": True, "identity_text": "custom"}
    agent_config_path = _wire_agent(tmp_path, monkeypatch, raw)

    config = load_agent_config("agent")
    persisted = json.loads(agent_config_path.read_text(encoding="utf-8"))

    assert config.codeact.enabled is True
    assert config.codeact.tool_routing == "repl-only"
    assert config.codeact.identity_text == "custom"
    assert config.general_harness is None
    assert "general_harness" not in persisted
    assert persisted["codeact"]["enabled"] is True
    backups = list(agent_config_path.parent.glob("*.codeact-migrate.bak"))
    assert len(backups) == 1


def _patched_profile(monkeypatch, profile):
    saved = {}
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config",
        lambda agent_id: profile,
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.save_agent_config",
        lambda agent_id, cfg: saved.update(agent_id=agent_id, cfg=cfg),
    )
    return saved


def _assert_pa_stack_off(cfg):
    assert cfg.system_prompt_files == []
    assert cfg.language == "en"
    assert cfg.running.memory_manager_backend == "none"
    assert cfg.running.light_context_config.strategy == "scroll"
    assert cfg.running.reme_light_memory_config.dream_cron_enabled is False


def test_apply_codeact_mode_repl_only(monkeypatch):
    from qwenpaw.config.codeact import apply_codeact_mode

    saved = _patched_profile(monkeypatch, AgentProfileConfig(id="a", name="a"))

    apply_codeact_mode("a", project_dir="/work")

    cfg = saved["cfg"]
    assert cfg.codeact.enabled is True
    assert cfg.codeact.tool_routing == "repl-only"
    assert cfg.coding_mode.enabled is True
    assert cfg.coding_mode.persona == "minimal"
    assert cfg.project_dir == "/work"
    _assert_pa_stack_off(cfg)


def test_apply_codeact_mode_hybrid(monkeypatch):
    from qwenpaw.config.codeact import apply_codeact_mode

    saved = _patched_profile(monkeypatch, AgentProfileConfig(id="a", name="a"))

    apply_codeact_mode("a", tool_routing="hybrid", identity_text="custom")

    cfg = saved["cfg"]
    assert cfg.codeact.tool_routing == "hybrid"
    assert cfg.codeact.identity_text == "custom"
    assert cfg.coding_mode.enabled is True
    assert cfg.coding_mode.persona == "full"
    _assert_pa_stack_off(cfg)


def test_apply_codeact_mode_off_disables_coding_mode(monkeypatch):
    from qwenpaw.config.codeact import apply_codeact_mode
    from qwenpaw.config.config import CodingModeConfig

    profile = AgentProfileConfig(id="a", name="a")
    profile.coding_mode = CodingModeConfig(enabled=True, persona="full")
    saved = _patched_profile(monkeypatch, profile)

    apply_codeact_mode("a", tool_routing="off")

    cfg = saved["cfg"]
    assert cfg.codeact.enabled is True
    assert cfg.codeact.tool_routing == "off"
    assert cfg.coding_mode.enabled is False
    _assert_pa_stack_off(cfg)


def test_apply_codeact_mode_rejects_unknown_routing(monkeypatch):
    import pytest

    from qwenpaw.config.codeact import apply_codeact_mode

    _patched_profile(monkeypatch, AgentProfileConfig(id="a", name="a"))

    with pytest.raises(ValueError, match="tool_routing"):
        apply_codeact_mode("a", tool_routing="bogus")


def test_apply_codeact_mode_scroll_off_keeps_native_strategy(monkeypatch):
    """PTC-only ablation arm: repl-only routing without the scroll stack."""
    from qwenpaw.config.codeact import apply_codeact_mode

    saved = _patched_profile(monkeypatch, AgentProfileConfig(id="a", name="a"))

    apply_codeact_mode("a", scroll=False)

    cfg = saved["cfg"]
    assert cfg.codeact.enabled is True
    assert cfg.codeact.tool_routing == "repl-only"
    assert cfg.coding_mode.enabled is True
    assert cfg.coding_mode.persona == "minimal"
    assert cfg.running.light_context_config.strategy == "native"
    assert cfg.system_prompt_files == []
    assert cfg.language == "en"
    assert cfg.running.memory_manager_backend == "none"
