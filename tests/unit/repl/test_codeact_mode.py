"""Three-state CodeAct mode resolution and prompt injection (roadmap §2.1/§2.2)."""

from __future__ import annotations

from types import SimpleNamespace

from qwenpaw.repl.prompt import CODEACT_SYSTEM_PROMPT
from qwenpaw.runtime.builder import (
    CODEACT_MODE_AUTO,
    CODEACT_MODE_OFF,
    CODEACT_MODE_REQUIRED,
    AgentBuilder,
    _resolve_codeact_mode,
    _toolkit_has_repl_exec,
)


def _toolkit(*names: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_groups=[
            SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names]),
        ],
    )


class TestResolveCodeactMode:
    def test_default_is_auto(self) -> None:
        assert _resolve_codeact_mode({}, None) == CODEACT_MODE_AUTO
        assert _resolve_codeact_mode(None, None) == CODEACT_MODE_AUTO

    def test_explicit_modes_win(self) -> None:
        for mode in ("off", "auto", "required"):
            assert _resolve_codeact_mode({"codeact_mode": mode}, None) == mode

    def test_explicit_mode_is_normalized(self) -> None:
        assert _resolve_codeact_mode({"codeact_mode": " REQUIRED "}, None) == (
            CODEACT_MODE_REQUIRED
        )

    def test_legacy_repl_only_maps_to_required(self) -> None:
        assert (
            _resolve_codeact_mode({"codeact_repl_only": True}, None)
            == CODEACT_MODE_REQUIRED
        )
        # The explicit mode still wins over the legacy flag.
        assert (
            _resolve_codeact_mode(
                {"codeact_repl_only": True, "codeact_mode": "auto"},
                None,
            )
            == CODEACT_MODE_AUTO
        )

    def test_invalid_value_falls_back_to_auto(self) -> None:
        assert _resolve_codeact_mode({"codeact_mode": "sometimes"}, None) == (
            CODEACT_MODE_AUTO
        )
        assert _resolve_codeact_mode({"codeact_mode": 3}, None) == (
            CODEACT_MODE_AUTO
        )

    def test_agent_config_default_is_honored(self) -> None:
        config = SimpleNamespace(codeact_mode="off")
        assert _resolve_codeact_mode({}, config) == CODEACT_MODE_OFF
        # Request context overrides the configured default.
        assert (
            _resolve_codeact_mode({"codeact_mode": "auto"}, config)
            == CODEACT_MODE_AUTO
        )


class TestCodeactPromptInjection:
    def test_append_when_mode_active_and_repl_exposed(self) -> None:
        toolkit = _toolkit("repl_exec", "read_file")
        result = AgentBuilder._append_codeact_prompt(
            "base prompt",
            CODEACT_MODE_AUTO,
            toolkit,
        )
        assert result.startswith("base prompt")
        assert CODEACT_SYSTEM_PROMPT in result

    def test_no_append_when_mode_off(self) -> None:
        toolkit = _toolkit("repl_exec")
        assert AgentBuilder._append_codeact_prompt(
            "base prompt",
            CODEACT_MODE_OFF,
            toolkit,
        ) == "base prompt"

    def test_no_append_when_repl_not_registered(self) -> None:
        toolkit = _toolkit("read_file", "write_file")
        assert AgentBuilder._append_codeact_prompt(
            "base prompt",
            CODEACT_MODE_AUTO,
            toolkit,
        ) == "base prompt"

    def test_no_append_without_toolkit(self) -> None:
        assert AgentBuilder._append_codeact_prompt(
            "base prompt",
            CODEACT_MODE_REQUIRED,
            None,
        ) == "base prompt"

    def test_toolkit_probe(self) -> None:
        assert _toolkit_has_repl_exec(_toolkit("repl_exec")) is True
        assert _toolkit_has_repl_exec(_toolkit("other")) is False
        assert _toolkit_has_repl_exec(None) is False


def test_codeact_prompt_covers_roadmap_rules() -> None:
    text = CODEACT_SYSTEM_PROMPT
    assert "persist" in text
    assert "restore_var" in text
    assert "peek" in text
    assert "permission_denied" in text
    assert "validation_error" in text
    assert "rate_limited" in text
