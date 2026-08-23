# -*- coding: utf-8 -*-
"""Request-scoped runtime binding for the CodeAct tool."""

# pylint: disable=protected-access,unnecessary-lambda

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.config.context import set_current_workspace_dir
from qwenpaw.repl import tool_def
from qwenpaw.repl.tool_def import (
    REPL_DESCRIPTION,
    bind_repl_exec_runtime,
    make_repl_exec_tool,
    make_repl_only_toolkit,
)


def test_repl_description_explains_help_lint_and_side_effect_priority() -> (
    None
):
    assert (
        "help(tool) prints documentation and returns None" in REPL_DESCRIPTION
    )
    assert "except Exception: pass" in REPL_DESCRIPTION
    assert (
        "Complete required writes and external mutations" in REPL_DESCRIPTION
    )


@pytest.mark.asyncio
async def test_repl_uses_bound_runtime_when_contextvars_are_missing(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    governor = SimpleNamespace(
        coding_project_dir=project,
        workspace_dir=tmp_path / "internal",
    )
    repl_exec = make_repl_exec_tool(governor)
    wrapper = SimpleNamespace(name="repl_exec", _func=repl_exec)
    toolkit = SimpleNamespace(
        tool_groups=[SimpleNamespace(tools=[wrapper])],
    )
    agent_state = SimpleNamespace()
    observed = {}

    class _Bridge:
        specs = []

    async def _from_current_context(**kwargs):
        observed["bridge"] = kwargs
        return _Bridge()

    class _Manager:
        async def get_or_start(self, **kwargs):
            observed["kernel"] = kwargs
            return SimpleNamespace()

        async def execute(self, _handle, code, _bridge, **kwargs):
            observed["code"] = code
            observed["execute_kwargs"] = kwargs
            return SimpleNamespace(
                ok=True,
                stdout="2\n",
                spilled=(),
                traceback="",
                vars_delta=(),
                tool_trace=(),
                kernel_restarted=False,
                error=None,
            )

    monkeypatch.setattr(
        tool_def.GovernanceBridge,
        "from_current_context",
        _from_current_context,
    )
    monkeypatch.setattr(
        tool_def,
        "get_default_kernel_manager",
        lambda: _Manager(),
    )
    set_current_workspace_dir(None)

    assert bind_repl_exec_runtime(toolkit, agent_state) == 1
    result = await repl_exec("1 + 1")

    assert result.state.value == "success"
    assert observed["bridge"]["workspace"] == project.resolve()
    assert observed["bridge"]["toolkit"] is toolkit
    assert observed["bridge"]["agent_state"] is agent_state
    assert observed["kernel"]["workspace"] == project.resolve()
    assert observed["code"] == "1 + 1"


def test_repl_prefers_governed_coding_project(tmp_path) -> None:
    project = tmp_path / "project"
    internal = tmp_path / "internal"
    governor = SimpleNamespace(
        coding_project_dir=project,
        workspace_dir=internal,
    )

    repl_exec = make_repl_exec_tool(governor)
    binding = repl_exec._repl_runtime_binding

    assert binding.workspace == project.resolve()


def test_repl_only_toolkit_hides_forwarded_tools() -> None:
    repl = SimpleNamespace(name="repl_exec")
    loca = SimpleNamespace(name="loca__canvas_list_courses")
    recall_structured = SimpleNamespace(name="recall_history")
    full_toolkit = SimpleNamespace(
        tool_groups=[SimpleNamespace(tools=[repl, loca, recall_structured])],
    )

    model_toolkit = make_repl_only_toolkit(full_toolkit)

    assert [tool.name for tool in model_toolkit.tool_groups[0].tools] == [
        "repl_exec",
    ]


def test_repl_only_toolkit_keeps_recall_history_python() -> None:
    repl = SimpleNamespace(name="repl_exec")
    recall_python = SimpleNamespace(name="recall_history_python")
    loca = SimpleNamespace(name="loca__canvas_list_courses")
    full_toolkit = SimpleNamespace(
        tool_groups=[SimpleNamespace(tools=[repl, recall_python, loca])],
    )

    model_toolkit = make_repl_only_toolkit(full_toolkit)

    assert [tool.name for tool in model_toolkit.tool_groups[0].tools] == [
        "repl_exec",
        "recall_history_python",
    ]


@pytest.mark.asyncio
async def test_repl_binding_can_forward_through_a_hidden_full_toolkit(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    repl_exec = make_repl_exec_tool(
        SimpleNamespace(coding_project_dir=project),
    )
    model_toolkit = SimpleNamespace(
        tool_groups=[
            SimpleNamespace(
                tools=[SimpleNamespace(name="repl_exec", _func=repl_exec)],
            ),
        ],
    )
    forwarding_toolkit = SimpleNamespace(name="full")
    agent_state = SimpleNamespace()
    observed = {}

    class _Bridge:
        specs = []

    async def _from_current_context(**kwargs):
        observed.update(kwargs)
        return _Bridge()

    class _Manager:
        async def get_or_start(self, **_kwargs):
            return SimpleNamespace()

        async def execute(self, _handle, _code, _bridge, **_kwargs):
            return SimpleNamespace(
                ok=True,
                stdout="ok",
                spilled=(),
                traceback="",
                vars_delta=(),
                tool_trace=(),
                kernel_restarted=False,
                error=None,
            )

    monkeypatch.setattr(
        tool_def.GovernanceBridge,
        "from_current_context",
        _from_current_context,
    )
    monkeypatch.setattr(
        tool_def,
        "get_default_kernel_manager",
        lambda: _Manager(),
    )

    bind_repl_exec_runtime(
        model_toolkit,
        agent_state,
        forwarding_toolkit=forwarding_toolkit,
    )
    await repl_exec("1")

    assert observed["toolkit"] is forwarding_toolkit
    assert observed["agent_state"] is agent_state


@pytest.mark.asyncio
async def test_repl_enforces_request_scoped_cell_budget(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    repl_exec = make_repl_exec_tool(
        SimpleNamespace(coding_project_dir=project),
    )
    toolkit = SimpleNamespace(
        tool_groups=[
            SimpleNamespace(
                tools=[SimpleNamespace(name="repl_exec", _func=repl_exec)],
            ),
        ],
    )
    executions = []

    class _Bridge:
        specs = []

    async def _from_current_context(**_kwargs):
        return _Bridge()

    class _Manager:
        async def get_or_start(self, **_kwargs):
            return SimpleNamespace()

        async def execute(self, _handle, code, _bridge, **_kwargs):
            executions.append(code)
            return SimpleNamespace(
                ok=True,
                stdout="ok",
                spilled=(),
                traceback="",
                vars_delta=(),
                tool_trace=(),
                kernel_restarted=False,
                error=None,
            )

    monkeypatch.setattr(
        tool_def.GovernanceBridge,
        "from_current_context",
        _from_current_context,
    )
    monkeypatch.setattr(
        tool_def,
        "get_default_kernel_manager",
        lambda: _Manager(),
    )
    bind_repl_exec_runtime(
        toolkit,
        SimpleNamespace(),
        request_context={
            "codeact_repl_soft_limit": 2,
            "codeact_repl_hard_limit": 3,
        },
    )

    first = await repl_exec("cell-1")
    second = await repl_exec("cell-2")
    third = await repl_exec("cell-3")
    rejected = await repl_exec("cell-4")

    assert "[repl budget]" not in first.content[0].text
    assert "Cell 2/3 used" in second.content[0].text
    assert "final executable cell" in third.content[0].text
    assert rejected.state.value == "error"
    assert "budget exhausted" in rejected.content[0].text
    assert rejected.metadata["budget_exhausted"] is True
    assert executions == ["cell-1", "cell-2", "cell-3"]
