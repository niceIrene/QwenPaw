"""Session-scoped kernel identity and telemetry paths (roadmap §2.5, §2.10)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from qwenpaw.repl.kernel_manager import (
    ExecResult,
    KernelHandle,
    KernelManager,
    _kernel_key,
    _session_tag,
)


class TestSessionKernelKey:
    def test_session_tag_is_stable_sha_prefix(self) -> None:
        session_id = "session-abc"
        expected = hashlib.sha256(session_id.encode()).hexdigest()[:12]
        assert _session_tag(session_id) == expected
        assert _session_tag(session_id) == _session_tag(session_id)

    def test_different_sessions_get_different_tags(self) -> None:
        assert _session_tag("one") != _session_tag("two")

    def test_empty_session_keeps_workspace_key(self) -> None:
        assert _kernel_key("ws-1", "") == "ws-1"
        assert _kernel_key("ws-1") == "ws-1"

    def test_session_key_format(self) -> None:
        key = _kernel_key("ws-1", "session-abc")
        tag = _session_tag("session-abc")
        assert key == f"ws-1::{tag}"
        assert key != "ws-1"

    def test_same_workspace_distinct_sessions(self) -> None:
        assert _kernel_key("ws-1", "a") != _kernel_key("ws-1", "b")
        assert _kernel_key("ws-1", "a") == _kernel_key("ws-1", "a")

    def test_same_session_distinct_workspaces(self) -> None:
        assert _kernel_key("ws-1", "a") != _kernel_key("ws-2", "a")


class TestExecResultErrorField:
    def test_from_message_parses_structured_error(self) -> None:
        error = {
            "kind": "runtime_error",
            "code": "ValueError",
            "message": "bad",
            "retryable": True,
            "suggestion": "fix it",
        }
        result = ExecResult.from_message(
            {
                "ok": False,
                "stdout": "",
                "error": error,
                "traceback": "tb",
            },
        )
        assert result.ok is False
        assert result.error == error
        assert result.traceback == "tb"

    def test_from_message_without_error(self) -> None:
        result = ExecResult.from_message({"ok": True, "stdout": "1"})
        assert result.ok is True
        assert result.error is None

    def test_from_message_ignores_malformed_error(self) -> None:
        result = ExecResult.from_message({"ok": False, "error": "oops"})
        assert result.error is None


class TestTelemetry:
    def _handle(self, workspace: Path) -> KernelHandle:
        return KernelHandle(
            workspace_id="ws-x",
            workspace=workspace,
            process=None,  # type: ignore[arg-type]
            specs=[],
            specs_hash="",
            session_id="session-x",
            key=_kernel_key("ws-x", "session-x"),
        )

    def test_append_telemetry_writes_jsonl(self, tmp_path: Path) -> None:
        manager = KernelManager(idle_timeout=0)
        handle = self._handle(tmp_path)
        handle.cell_index = 3

        manager._append_telemetry(  # pylint: disable=protected-access
            handle,
            exec_id="e-1",
            ok=True,
            error_kind="",
            duration_ms=12.5,
            tool_calls=[{"tool": "lookup", "status": "ok", "duration_ms": 2}],
            output_bytes=4,
            spilled=[],
            vars_delta=["x"],
            kernel_restarted=False,
        )

        path = tmp_path / ".qwenpaw-repl" / "telemetry.jsonl"
        assert path.is_file()
        (record,) = [
            json.loads(line) for line in path.read_text().splitlines() if line
        ]
        assert record["session_id"] == "session-x"
        assert record["workspace_id"] == "ws-x"
        assert record["exec_id"] == "e-1"
        assert record["cell_index"] == 3
        assert record["ok"] is True
        assert record["error_kind"] == ""
        assert record["duration_ms"] == 12.5
        assert record["tool_calls"][0]["tool"] == "lookup"
        assert record["output_bytes"] == 4
        assert record["vars_delta"] == ["x"]
        assert record["kernel_restarted"] is False
        assert "ts" in record

    def test_append_telemetry_appends(self, tmp_path: Path) -> None:
        manager = KernelManager(idle_timeout=0)
        handle = self._handle(tmp_path)
        for index in range(2):
            manager._append_telemetry(  # pylint: disable=protected-access
                handle,
                exec_id=f"e-{index}",
                ok=True,
                error_kind="",
                duration_ms=1.0,
                tool_calls=[],
                output_bytes=0,
                spilled=[],
                vars_delta=[],
                kernel_restarted=False,
            )
        path = tmp_path / ".qwenpaw-repl" / "telemetry.jsonl"
        assert len(path.read_text().splitlines()) == 2
