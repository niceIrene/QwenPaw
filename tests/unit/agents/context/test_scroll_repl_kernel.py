# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Kernel backend of ``recall_history_python``.

When a governor is supplied and the CodeAct kernel's strict sandbox is
usable, recall cells run in the shared persistent kernel instead of a fresh
subprocess per call. These tests pin the backend selection, the shim bridge
(specs untouched, paw.* blocked), and the observation mapping that must never
let a failure read as an empty history. The kernel manager is faked; real
kernel execution is covered by ``tests/unit/repl/test_kernel_manager.py``.
"""

from types import SimpleNamespace

import pytest
from agentscope.message import ToolResultState

from qwenpaw.agents.context.scroll import repl as scroll_repl
from qwenpaw.repl import kernel_manager as km
from qwenpaw.repl.governance_bridge import ToolForwardingError


class _FakeManager:
    def __init__(self, result=None, start_error=None):
        self.result = result
        self.start_error = start_error
        self.started = None
        self.executed = None

    async def get_or_start(self, **kwargs):
        if self.start_error is not None:
            raise self.start_error
        self.started = kwargs
        return SimpleNamespace(specs=[{"path": "lookup", "name": "lookup"}])

    async def execute(self, handle, code, bridge, **kwargs):
        self.executed = {"handle": handle, "code": code, "bridge": bridge}
        self.exec_kwargs = kwargs
        return self.result


def _governor(tmp_path):
    return SimpleNamespace(
        coding_project_dir=None,
        workspace_dir=str(tmp_path),
    )


def _make_tool(tmp_path, governor):
    return scroll_repl.make_recall_history_python(
        history_db_path=str(tmp_path / "history.db"),
        session_id="sess-1",
        agent_id="agent-1",
        scratch_root=str(tmp_path / ".scroll"),
        governor=governor,
    )


def _kernel_ready(monkeypatch):
    monkeypatch.setattr(
        km,
        "repl_sandbox_available",
        lambda _governor, preflight=False: (True, "ok"),
    )


def _patch_manager(monkeypatch, manager):
    monkeypatch.setattr(km, "get_default_kernel_manager", lambda: manager)


@pytest.mark.asyncio
async def test_kernel_backend_executes_in_shared_kernel(
    monkeypatch,
    tmp_path,
):
    _kernel_ready(monkeypatch)
    manager = _FakeManager(result=km.ExecResult(ok=True, stdout="3 rows"))
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="print('3 rows')")

    assert chunk.state == ToolResultState.SUCCESS
    assert "3 rows" in chunk.content[0].text
    code = manager.executed["code"]
    assert "MemorySpace" in code
    assert "print('3 rows')" in code
    # ms is cached: rebuilt only when missing or clobbered.
    assert "isinstance(globals().get('ms')" in code
    assert "'sess-1'" in code and "'agent-1'" in code
    # Same workspace+session keying as repl_exec.
    assert manager.started["session_id"] == "sess-1"
    assert manager.started["governor"] is not None
    # Shim bridge keeps kernel specs untouched and blocks paw.* calls.
    bridge = manager.executed["bridge"]
    assert bridge.specs == [{"path": "lookup", "name": "lookup"}]
    assert bridge.is_read_only("lookup") is False
    with pytest.raises(ToolForwardingError, match="not available inside"):
        await bridge.dispatch("lookup", {}, kernel_task_id="e-1")


@pytest.mark.asyncio
async def test_kernel_unavailable_falls_back_to_legacy_fail_closed(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        km,
        "repl_sandbox_available",
        lambda _governor, preflight=False: (False, "no bwrap"),
    )
    manager = _FakeManager()
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="print(1)")

    # Legacy path with no sandbox_config and no opt-in fails closed.
    assert chunk.state == ToolResultState.DENIED
    assert "refused" in chunk.content[0].text
    assert manager.executed is None


@pytest.mark.asyncio
async def test_kernel_start_failure_falls_back_to_legacy(
    monkeypatch,
    tmp_path,
):
    _kernel_ready(monkeypatch)
    manager = _FakeManager(start_error=km.KernelUnavailableError("boom"))
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="print(1)")

    assert chunk.state == ToolResultState.DENIED
    assert "refused" in chunk.content[0].text


@pytest.mark.asyncio
async def test_kernel_busy_maps_to_retryable_guidance(
    monkeypatch,
    tmp_path,
):
    _kernel_ready(monkeypatch)
    manager = _FakeManager(
        result=km.ExecResult(
            ok=False,
            error={
                "code": "kernel_busy",
                "message": "busy",
                "retryable": True,
            },
        ),
    )
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="print(1)")

    assert chunk.state == ToolResultState.ERROR
    text = chunk.content[0].text
    assert "RECALL BUSY" in text
    assert "did NOT run" in text
    assert "retry" in text


@pytest.mark.asyncio
async def test_kernel_failure_never_reads_as_empty_history(
    monkeypatch,
    tmp_path,
):
    _kernel_ready(monkeypatch)
    manager = _FakeManager(
        result=km.ExecResult(ok=False, traceback="Traceback: boom"),
    )
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="print(1)")

    assert chunk.state == ToolResultState.ERROR
    text = chunk.content[0].text
    assert "RECALL FAILED" in text
    assert "NOT read" in text
    assert "Traceback: boom" in text


@pytest.mark.asyncio
async def test_kernel_partial_stdout_then_failure_is_incomplete(
    monkeypatch,
    tmp_path,
):
    _kernel_ready(monkeypatch)
    manager = _FakeManager(
        result=km.ExecResult(
            ok=False,
            stdout="partial rows",
            traceback="Traceback: boom",
        ),
    )
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="print(1)")

    assert chunk.state == ToolResultState.ERROR
    text = chunk.content[0].text
    assert "RECALL INCOMPLETE" in text
    assert "partial rows" in text


@pytest.mark.asyncio
async def test_kernel_ok_but_silent_is_not_evidence(
    monkeypatch,
    tmp_path,
):
    _kernel_ready(monkeypatch)
    manager = _FakeManager(result=km.ExecResult(ok=True, stdout=""))
    _patch_manager(monkeypatch, manager)
    tool = _make_tool(tmp_path, _governor(tmp_path))

    chunk = await tool(source="x = 1")

    assert chunk.state == ToolResultState.SUCCESS
    assert "no output" in chunk.content[0].text


@pytest.mark.asyncio
async def test_no_governor_uses_legacy_path_only(monkeypatch, tmp_path):
    manager = _FakeManager()
    _patch_manager(monkeypatch, manager)
    tool = scroll_repl.make_recall_history_python(
        history_db_path=str(tmp_path / "history.db"),
        session_id="sess-1",
        scratch_root=str(tmp_path / ".scroll"),
    )

    chunk = await tool(source="print(1)")

    assert chunk.state == ToolResultState.DENIED
    assert manager.executed is None


# ── build-time wiring: governor → mounts + kernel backend ──


def _scroll_agent_config() -> SimpleNamespace:
    return SimpleNamespace(
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                strategy="scroll",
                scroll_config=SimpleNamespace(
                    db_filename="history.db",
                    offload_dialog=False,
                    repl_timeout_s=300,
                    allow_unsandboxed=False,
                ),
                tool_result_pruning_config=SimpleNamespace(
                    pruning_recent_msg_max_bytes=1000,
                ),
            ),
        ),
    )


def test_build_components_registers_kernel_mounts(tmp_path):
    from qwenpaw.agents.context import build_scroll_components

    governor = SimpleNamespace()
    components = build_scroll_components(
        agent_config=_scroll_agent_config(),
        workspace_dir=str(tmp_path),
        model=None,
        session_id="s1",
        agent_id="a1",
        governor=governor,
    )

    assert components is not None
    scratch = (tmp_path / ".scroll").resolve()
    assert scratch.is_dir()
    by_path = {m.path: m for m in governor._repl_extra_mounts}
    assert by_path[str(tmp_path.resolve())].writable is False
    assert by_path[str(scratch)].writable is True


def test_build_components_without_governor_registers_nothing(tmp_path):
    from qwenpaw.agents.context import build_scroll_components

    components = build_scroll_components(
        agent_config=_scroll_agent_config(),
        workspace_dir=str(tmp_path),
        model=None,
        session_id="s1",
        agent_id="a1",
    )

    assert components is not None
    assert not (tmp_path / ".scroll").exists()
