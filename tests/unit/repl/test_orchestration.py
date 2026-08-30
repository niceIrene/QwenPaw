# -*- coding: utf-8 -*-
"""Orchestration mode: env parsing, plan gate, prompt and tool selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.repl.exec_server import _PlanGate
from qwenpaw.repl.orchestration import (
    ORCHESTRATION_STDOUT_LIMIT,
    code_tool_name,
    orchestration_mode,
)
from qwenpaw.repl.prompt import (
    CODEACT_ORCHESTRATION_AUTO_RULE,
    CODEACT_ORCHESTRATION_FORCED_RULE,
    CODEACT_SYSTEM_PROMPT,
    codeact_prompt_section,
)


def test_orchestration_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("QWENPAW_ORCHESTRATION", raising=False)
    assert orchestration_mode() is None
    assert code_tool_name() == "repl_exec"


def test_orchestration_mode_parses_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv("QWENPAW_ORCHESTRATION", " Forced ")
    assert orchestration_mode() == "forced"
    assert code_tool_name() == "orchestrate_python"
    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "banana")
    assert orchestration_mode() is None
    assert code_tool_name() == "repl_exec"


def test_plan_gate_rejects_cells_without_plan():
    gate = _PlanGate(required=True)
    error = gate.check("print('hello')")
    assert error is not None
    assert error["code"] == "plan_required"
    assert error["retryable"] is True


def test_plan_gate_accepts_assignment_and_closes():
    gate = _PlanGate(required=True)
    assert gate.check("PLAN = 'recon -> fan-out -> aggregate'") is None
    namespace: dict[str, object] = {}
    exec("PLAN = 'recon -> fan-out -> aggregate'", namespace)  # noqa: S102
    gate.update(namespace)
    assert gate.done
    assert gate.check("print('now anything goes')") is None


def test_plan_gate_ignores_nested_or_non_string_plan():
    gate = _PlanGate(required=True)
    # PLAN assigned only inside a function body does not count.
    assert gate.check("def f():\n    PLAN = 'x'\n") is not None
    # A non-string PLAN must not close the gate.
    assert gate.check("PLAN = 42") is None
    namespace: dict[str, object] = {}
    exec("PLAN = 42", namespace)  # noqa: S102
    gate.update(namespace)
    assert not gate.done


def test_plan_gate_not_required_is_open():
    gate = _PlanGate(required=False)
    assert gate.done
    assert gate.check("print('anything')") is None


def test_orchestration_prompt_forced_mentions_plan_gate(monkeypatch):
    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "forced")
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "dashscope/qwen3.8-27b")
    section = codeact_prompt_section()
    assert CODEACT_ORCHESTRATION_FORCED_RULE.strip() in section
    assert "orchestrate_python" in section
    assert "`repl_exec`" not in section


def test_orchestration_prompt_auto_encourages_delegation(monkeypatch):
    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "auto")
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "dashscope/qwen3.8-27b")
    section = codeact_prompt_section()
    assert CODEACT_ORCHESTRATION_AUTO_RULE.strip() in section
    assert "PLAN" not in section.split("Orchestration rules")[0]


def test_orchestration_prompt_falls_back_without_small_model(monkeypatch):
    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "forced")
    monkeypatch.delenv("QWENPAW_SMALL_MODEL", raising=False)
    assert codeact_prompt_section() == CODEACT_SYSTEM_PROMPT


def test_orchestration_tool_description_and_name(monkeypatch):
    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "auto")
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "dashscope/qwen3.8-27b")
    from qwenpaw.repl.tool_def import (
        make_repl_exec_tool,
        orchestrate_description,
    )

    description = orchestrate_description()
    assert "paw.lm" in description
    assert "~2 KB" in description
    assert "orchestrate_python" in description

    governor = SimpleNamespace(
        coding_project_dir=None,
        workspace_dir=None,
    )
    tool = make_repl_exec_tool(governor)
    assert tool.__name__ == "orchestrate_python"
    assert tool._tool_descriptor.name == "orchestrate_python"


def test_plain_tool_untouched_without_orchestration(monkeypatch):
    monkeypatch.delenv("QWENPAW_ORCHESTRATION", raising=False)
    from qwenpaw.repl.tool_def import make_repl_exec_tool

    governor = SimpleNamespace(
        coding_project_dir=None,
        workspace_dir=None,
    )
    tool = make_repl_exec_tool(governor)
    assert tool.__name__ == "repl_exec"
    assert tool._tool_descriptor.name == "repl_exec"


def test_kernel_init_config_orchestration(monkeypatch):
    from qwenpaw.repl import kernel_manager

    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "forced")
    manager = kernel_manager.KernelManager()
    config = manager._build_init_config("default")
    assert config["plan_required"] is True
    assert config["stdout_limit"] == ORCHESTRATION_STDOUT_LIMIT

    monkeypatch.setenv("QWENPAW_ORCHESTRATION", "auto")
    config = manager._build_init_config("default")
    assert config["plan_required"] is False
    assert config["stdout_limit"] == ORCHESTRATION_STDOUT_LIMIT

    monkeypatch.delenv("QWENPAW_ORCHESTRATION")
    config = manager._build_init_config("default")
    assert "plan_required" not in config
    assert config["stdout_limit"] > ORCHESTRATION_STDOUT_LIMIT


@pytest.mark.asyncio
async def test_run_cell_plan_gate_end_to_end(tmp_path):
    """A gated kernel rejects plan-less cells and accepts PLAN cells."""
    from qwenpaw.repl.exec_server import _run_cell

    channel = SimpleNamespace(tool_trace=[])
    gate = _PlanGate(required=True)

    rejected = _run_cell(
        {"id": "c1", "code": "x = 1"},
        {},
        channel,
        tmp_path,
        2048,
        plan_gate=gate,
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "plan_required"

    namespace: dict[str, object] = {}
    accepted = _run_cell(
        {"id": "c2", "code": "PLAN = 'recon -> aggregate'\nprint('planned')"},
        namespace,
        channel,
        tmp_path,
        2048,
        plan_gate=gate,
    )
    assert accepted["ok"] is True
    assert "planned" in accepted["stdout"]
    assert gate.done

    followup = _run_cell(
        {"id": "c3", "code": "print('free now')"},
        namespace,
        channel,
        tmp_path,
        2048,
        plan_gate=gate,
    )
    assert followup["ok"] is True
    assert "free now" in followup["stdout"]
