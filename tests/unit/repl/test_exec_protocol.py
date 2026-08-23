# -*- coding: utf-8 -*-
"""Exec-server protocol behavior.

Covers display, errors, interrupt, and restore (§2.3-§2.8).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from qwenpaw.repl import exec_server
from qwenpaw.repl.protocol import encode_message


def _serve(
    messages: list[dict],
    workspace: Path,
) -> list[dict]:
    """Drive the serve loop with pre-canned messages, return its output."""
    stdin = io.StringIO("".join(encode_message(m).decode() for m in messages))
    out = io.StringIO()
    exec_server.serve(stdin, out, workspace=workspace)
    out.seek(0)
    return [json.loads(line) for line in out if line.strip()]


def _init(session_tag: str) -> dict:
    return {
        "id": "init-1",
        "type": "init",
        "tools": [],
        "config": {"stdout_limit": 2048, "session_tag": session_tag},
    }


def _exec_results(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m["type"] == "exec_result"]


def test_successful_cell_returns_stdout_and_snapshot(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-a"),
            {"id": "e1", "type": "exec", "code": "x = 40 + 2\nx"},
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is True
    assert result["stdout"] == "42\n"
    assert result["vars_delta"] == [
        {"name": "x", "type": "int", "size": 28},
    ]
    manifest = tmp_path / ".qwenpaw-repl/snapshots/tag-a/latest/manifest.json"
    assert manifest.is_file()


def test_invalid_display_returns_validation_error(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-b"),
            {
                "id": "e1",
                "type": "exec",
                "code": "1",
                "display": "everything",
            },
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is False
    assert result["error"]["kind"] == "validation_error"
    assert result["error"]["code"] == "invalid_display"
    assert result["error"]["retryable"] is False


def test_syntax_error_is_structured(tmp_path: Path) -> None:
    sent = _serve(
        [_init("tag-c"), {"id": "e1", "type": "exec", "code": "def f("}],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is False
    assert result["error"]["kind"] == "syntax_error"
    assert result["traceback"]


def test_runtime_error_is_structured(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-d"),
            {"id": "e1", "type": "exec", "code": "1 / 0"},
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is False
    assert result["error"]["kind"] == "runtime_error"
    assert result["error"]["code"] == "ZeroDivisionError"
    assert result["error"]["retryable"] is True


def test_interrupt_stops_a_spinning_cell(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-e"),
            {
                "id": "e1",
                "type": "exec",
                "code": "import time\ntime.sleep(0.3)\nwhile True: pass",
            },
            {"id": "e1", "type": "interrupt"},
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is False
    assert result["error"]["kind"] == "interrupted"
    assert result.get("interrupted") is True


def test_display_none_suppresses_last_expression(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-f"),
            {
                "id": "e1",
                "type": "exec",
                "code": "12345",
                "display": "none",
            },
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is True
    assert result["stdout"] == ""


def test_display_full_keeps_complete_repr(tmp_path: Path) -> None:
    payload = "z" * 2000
    sent = _serve(
        [
            _init("tag-g"),
            {
                "id": "e1",
                "type": "exec",
                "code": f"'{payload}'",
                "display": "full",
            },
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is True
    # The repr of the value survives intact (output was spilled instead).
    assert payload in result["stdout"] or result["spilled"]


def test_snapshot_restore_round_trip(tmp_path: Path) -> None:
    _serve(
        [
            _init("tag-h"),
            {"id": "e1", "type": "exec", "code": "kept = {'a': 1}"},
        ],
        tmp_path,
    )
    sent = _serve(
        [
            _init("tag-h"),
            {"id": "r1", "type": "restore", "snapshot_id": "tag-h/latest"},
            {"id": "e1", "type": "exec", "code": "kept"},
        ],
        tmp_path,
    )
    (restore,) = [m for m in sent if m["type"] == "restore_result"]
    (result,) = _exec_results(sent)
    assert restore["ok"] is True
    assert restore["restored"] == ["kept"]
    assert result["ok"] is True
    assert result["stdout"].strip() == "{'a': 1}"


def test_unsafe_snapshot_id_is_rejected(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-i"),
            {"id": "r1", "type": "restore", "snapshot_id": "../escape"},
        ],
        tmp_path,
    )
    (restore,) = [m for m in sent if m["type"] == "restore_result"]
    assert restore["ok"] is False
    assert restore["error"]["kind"] == "validation_error"


def test_missing_snapshot_reports_failure(tmp_path: Path) -> None:
    sent = _serve(
        [
            _init("tag-j"),
            {"id": "r1", "type": "restore", "snapshot_id": "tag-j/latest"},
        ],
        tmp_path,
    )
    (restore,) = [m for m in sent if m["type"] == "restore_result"]
    assert restore["ok"] is False
    assert restore["error"]["code"] == "snapshot_missing"


def test_cell_flushed_even_when_stdin_closes_mid_cell(tmp_path: Path) -> None:
    # No explicit shutdown message: stdin EOF must not drop the result.
    sent = _serve(
        [
            _init("tag-k"),
            {
                "id": "e1",
                "type": "exec",
                "code": "import time\ntime.sleep(0.2)\n'done'",
            },
        ],
        tmp_path,
    )
    (result,) = _exec_results(sent)
    assert result["ok"] is True
    assert result["stdout"].strip() == "'done'"


def test_unknown_message_type_is_logged_not_fatal(tmp_path: Path) -> None:
    # encode_message refuses unknown types, so craft the bad line raw.
    valid = "".join(
        encode_message(m).decode()
        for m in [_init("tag-l"), {"id": "e1", "type": "exec", "code": "5"}]
    )
    bad_line = '{"id":"x1","type":"teleport"}\n'
    stdin = io.StringIO(valid + bad_line)
    out = io.StringIO()
    exec_server.serve(stdin, out, workspace=tmp_path)
    out.seek(0)
    sent = [json.loads(line) for line in out if line.strip()]

    # The valid cell still completes; the bad message logs and stops cleanly.
    (result,) = _exec_results(sent)
    assert result["ok"] is True
    logs = [m for m in sent if m["type"] == "log"]
    assert any(
        "protocol error" in m.get("message", "")
        and "teleport" in m.get("message", "")
        for m in logs
    )
