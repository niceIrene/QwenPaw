# -*- coding: utf-8 -*-
"""End-to-end paw.lm dispatch + tracing checks (design doc §3, §5).

Verifies the full path a benchmark trial depends on: kernel cell → lm_call
protocol message → kernel_manager dispatch → LMExecutor → lm_result →
cell-visible TaskResult, plus the observability contract (tool_trace entry
in the rendered observation, telemetry.jsonl record, prompt gating).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qwenpaw.repl.kernel_manager import (
    KernelManager,
    repl_sandbox_available,
)
from qwenpaw.repl.lm_executor import LMCallError, LMConfig, LMExecutor
from qwenpaw.repl.prompt import CODEACT_SYSTEM_PROMPT, codeact_prompt_section
from qwenpaw.repl.tool_def import REPL_DESCRIPTION, render_observation, repl_description
from qwenpaw.sandbox import probe_sandbox_support


class _RecordingExecutor:
    """Stand-in LMExecutor: records payloads, returns canned values."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, payload, *, workspace, session_id):
        self.calls.append(
            {
                "payload": dict(payload),
                "workspace": str(workspace),
                "session_id": session_id,
            },
        )
        if payload.get("op") == "map":
            return [
                {"status": "ok", "value": i, "raw": "", "usage": {}}
                for i, _ in enumerate(payload.get("items") or [])
            ]
        return {"status": "ok", "value": {"n": 2}, "raw": "", "usage": {}}


class _FailingExecutor:
    async def execute(self, payload, *, workspace, session_id):
        raise LMCallError(
            "budget_exhausted",
            "paw.lm call budget exhausted (200/200)",
            suggestion="Finish with retained results.",
        )


class _Bridge:
    specs: list[dict] = []

    def __init__(self, lm_executor=None) -> None:
        self.lm_executor = lm_executor

    async def dispatch(self, *_args, **_kwargs):
        raise AssertionError("no governed tool call was expected")


def _governor():
    capability = probe_sandbox_support()
    return SimpleNamespace(
        sandbox_usable=capability.supported,
        sandbox_globally_enabled=True,
        sandbox_capability=capability,
    )


def _skip_without_sandbox(governor) -> None:
    available, reason = repl_sandbox_available(governor, preflight=True)
    if not available:
        pytest.skip(reason)


# ------------------------------------------------------------ prompt gating
def test_prompts_byte_identical_without_small_model(monkeypatch):
    monkeypatch.delenv("QWENPAW_SMALL_MODEL", raising=False)
    assert codeact_prompt_section() == CODEACT_SYSTEM_PROMPT
    assert repl_description() == REPL_DESCRIPTION


def test_prompts_gain_lm_sections_when_configured(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "prov/model")
    assert codeact_prompt_section().startswith(CODEACT_SYSTEM_PROMPT)
    assert "paw.lm" in codeact_prompt_section()
    assert repl_description().startswith(REPL_DESCRIPTION)
    assert "paw.lm.call" in repl_description()


def test_prompt_mode_defaults_to_auto_trigger(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "prov/model")
    monkeypatch.delenv("QWENPAW_LM_PROMPT_MODE", raising=False)
    section = codeact_prompt_section()
    assert "Delegation trigger" in section
    assert "MANDATORY delegation" not in section


def test_prompt_mode_forced_selects_mandatory_rule(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "prov/model")
    monkeypatch.setenv("QWENPAW_LM_PROMPT_MODE", "forced")
    section = codeact_prompt_section()
    assert "MANDATORY delegation" in section
    assert "Delegation trigger" not in section
    monkeypatch.setenv("QWENPAW_LM_PROMPT_MODE", "auto")
    assert "Delegation trigger" in codeact_prompt_section()


def test_prompt_mode_ignored_without_small_model(monkeypatch):
    monkeypatch.delenv("QWENPAW_SMALL_MODEL", raising=False)
    monkeypatch.setenv("QWENPAW_LM_PROMPT_MODE", "forced")
    assert codeact_prompt_section() == CODEACT_SYSTEM_PROMPT


# ------------------------------------------------------------ executor wire
async def test_executor_passes_colon_slot_override_to_factory():
    seen: list[str] = []

    def factory(override: str):
        seen.append(override)
        raise RuntimeError("stop after override check")

    executor = LMExecutor(
        LMConfig(provider_id="prov", model="model"),
        model_factory=factory,
    )
    with pytest.raises(RuntimeError, match="stop after override check"):
        await executor._get_model()
    assert seen == ["prov:model"]


# ------------------------------------------------------- kernel round-trip
async def test_lm_call_roundtrip_tracing_and_telemetry(tmp_path):
    governor = _governor()
    _skip_without_sandbox(governor)
    manager = KernelManager(idle_timeout=0)
    try:
        handle = await manager.get_or_start(
            workspace_id="lm-trace-test",
            workspace=tmp_path,
            specs=[],
            governor=governor,
            session_id="lm-trace-session",
        )
        executor = _RecordingExecutor()
        bridge = _Bridge(lm_executor=executor)

        call_cell = await manager.execute(
            handle,
            "rows = ['alpha', 'beta']\n"
            "r = paw.lm.call('count rows', context=[paw.lm.var('rows')], "
            "schema={'type': 'object'})\n"
            "print(r.status, r.value)",
            bridge,
        )
        map_cell = await manager.execute(
            handle,
            "rs = paw.lm.map('classify', items=['a', 'b', 'c'])\n"
            "print([r.value for r in rs])",
            bridge,
        )

        assert call_cell.ok is True
        assert "ok {'n': 2}" in call_cell.stdout
        assert map_cell.ok is True
        assert "[0, 1, 2]" in map_cell.stdout

        # Executor saw the resolved context and the session/workspace pair.
        payload = executor.calls[0]["payload"]
        assert payload["op"] == "call"
        assert payload["context"][0]["kind"] == "var"
        assert json.loads(payload["context"][0]["value"]) == ["alpha", "beta"]
        assert executor.calls[0]["session_id"] == "lm-trace-session"
        assert executor.calls[1]["payload"]["op"] == "map"

        # Kernel-side tool_trace carries the paw.lm entries...
        assert {"tool": "paw.lm.call", "status": "ok"} in [
            dict(item) for item in call_cell.tool_trace
        ]
        assert {"tool": "paw.lm.map", "status": "ok"} in [
            dict(item) for item in map_cell.tool_trace
        ]

        # ...and renders into the observation exactly how the Terminal-Bench
        # runner's repl_lm_calls regex expects it.
        observation = render_observation(call_cell)
        assert "paw.lm.call[ok]" in observation
        assert "paw.lm.map[ok]" in render_observation(map_cell)

        # Main-process telemetry.jsonl records each cell with its lm calls.
        telemetry = tmp_path / ".qwenpaw-repl" / "telemetry.jsonl"
        records = [
            json.loads(line)
            for line in telemetry.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        traced = [
            entry
            for record in records
            for entry in record.get("tool_calls", [])
        ]
        lm_entries = [
            entry for entry in traced if entry["tool"].startswith("paw.lm.")
        ]
        assert {entry["tool"] for entry in lm_entries} == {
            "paw.lm.call",
            "paw.lm.map",
        }
        assert all(entry["status"] == "ok" for entry in lm_entries)
        assert all(entry["duration_ms"] >= 0 for entry in lm_entries)
    finally:
        await manager.close_all()


async def test_lm_error_surfaces_structured_kind_in_cell(tmp_path):
    governor = _governor()
    _skip_without_sandbox(governor)
    manager = KernelManager(idle_timeout=0)
    try:
        handle = await manager.get_or_start(
            workspace_id="lm-error-test",
            workspace=tmp_path,
            specs=[],
            governor=governor,
            session_id="lm-error-session",
        )
        bridge = _Bridge(lm_executor=_FailingExecutor())
        result = await manager.execute(
            handle,
            "paw.lm.call('anything')",
            bridge,
        )
        assert result.ok is False
        assert "budget_exhausted" in result.traceback
        assert dict(result.tool_trace[0]) == {
            "tool": "paw.lm.call",
            "status": "budget_exhausted",
        }
    finally:
        await manager.close_all()


async def test_lm_unavailable_without_executor(tmp_path):
    governor = _governor()
    _skip_without_sandbox(governor)
    manager = KernelManager(idle_timeout=0)
    try:
        handle = await manager.get_or_start(
            workspace_id="lm-off-test",
            workspace=tmp_path,
            specs=[],
            governor=governor,
            session_id="lm-off-session",
        )
        result = await manager.execute(
            handle,
            "paw.lm.call('anything')",
            _Bridge(lm_executor=None),
        )
        assert result.ok is False
        assert "lm_unavailable" in result.traceback
    finally:
        await manager.close_all()
