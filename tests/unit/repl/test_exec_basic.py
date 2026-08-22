"""Persistent execution and lint tests for the P0 exec server."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from qwenpaw.repl.exec_server import CellLintError, _run_cell, lint_tree
from qwenpaw.repl.protocol import encode_message
from qwenpaw.repl.proxy_runtime import build_namespace


class FakeChannel:
    def __init__(self) -> None:
        self.tool_trace: list[dict[str, str]] = []

    def call_tool(self, tool: str, args: dict):
        return {"tool": tool, "args": args}


def _cell(
    namespace: dict,
    channel: FakeChannel,
    workspace: Path,
    cell_id: str,
    code: str,
    *,
    limit: int = 4096,
) -> dict:
    return _run_cell(
        {"id": cell_id, "type": "exec", "code": code},
        namespace,
        channel,  # type: ignore[arg-type]
        workspace,
        limit,
    )


def test_variables_survive_across_cells_and_last_expr_is_rendered(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    namespace, _ = build_namespace(channel, tmp_path, [])

    first = _cell(namespace, channel, tmp_path, "e1", "items = [1, 2, 3]")
    second = _cell(namespace, channel, tmp_path, "e2", "sum(items)")

    assert first["ok"] is True
    assert any(item["name"] == "items" for item in first["vars_delta"])
    assert second["ok"] is True
    assert second["stdout"].strip() == "6"


def test_exception_surfaces_original_traceback(tmp_path: Path) -> None:
    channel = FakeChannel()
    namespace, _ = build_namespace(channel, tmp_path, [])

    result = _cell(namespace, channel, tmp_path, "e1", "1 / 0")

    assert result["ok"] is False
    assert "ZeroDivisionError" in result["traceback"]
    assert "<cell>" in result["traceback"]


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    1 / 0\nexcept:\n    pass",
        "try:\n    1 / 0\nexcept Exception:\n    ...",
        "os.system('echo unsafe')",
        "subprocess.run(['true'])",
    ],
)
def test_lint_rejects_forbidden_patterns(source: str) -> None:
    with pytest.raises(CellLintError):
        lint_tree(ast.parse(source))


def test_large_output_spills_and_context_stays_bounded(tmp_path: Path) -> None:
    channel = FakeChannel()
    namespace, _ = build_namespace(channel, tmp_path, [])

    result = _cell(
        namespace,
        channel,
        tmp_path,
        "e-large",
        "print('x' * (5 * 1024 * 1024))",
        limit=512,
    )

    assert result["ok"] is True
    assert len(result["stdout"].encode()) <= 512
    assert result["spilled"] == ["out/spill_e-large.txt"]
    assert "original REPL variables" in result["stdout"]
    assert "do not print the whole spill" in result["stdout"]
    assert (tmp_path / result["spilled"][0]).stat().st_size > 5 * 1024 * 1024
    assert len(encode_message(result)) < 8 * 1024


def test_direct_sys_stdout_writes_cannot_corrupt_protocol(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    namespace, _ = build_namespace(channel, tmp_path, [])

    result = _cell(
        namespace,
        channel,
        tmp_path,
        "e-direct-stdout",
        "import sys\nsys.stdout.write('x' * (1024 * 1024))",
        limit=512,
    )

    assert result["ok"] is True
    assert result["spilled"] == ["out/spill_e-direct-stdout.txt"]
    assert len(encode_message(result)) < 8 * 1024
