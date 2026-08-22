"""Kernel proxy discovery, signatures, and workspace helper tests."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from qwenpaw.repl.proxy_runtime import build_namespace


class RecordingChannel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, tool: str, args: dict):
        self.calls.append((tool, args))
        return {"ok": True}


TOOLS = [
    {
        "path": "mcp.mail.search",
        "name": "mail__search",
        "description": "Search mail.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
]


def test_proxy_has_typed_signature_doc_and_roundtrip(tmp_path: Path) -> None:
    channel = RecordingChannel()
    namespace, _ = build_namespace(channel, tmp_path, TOOLS)
    search = namespace["paw"].tools.mcp.mail.search

    signature = inspect.signature(search)
    assert signature.parameters["query"].annotation is str
    assert signature.parameters["max_results"].default == 10
    assert "Search mail" in inspect.getdoc(search)
    assert search("urgent", max_results=3) == {"ok": True}
    assert channel.calls == [
        ("mcp.mail.search", {"query": "urgent", "max_results": 3}),
    ]


def test_dir_progressively_discloses_tools(tmp_path: Path) -> None:
    namespace, _ = build_namespace(RecordingChannel(), tmp_path, TOOLS)
    assert "mcp" in dir(namespace["paw"].tools)
    assert "mail" in dir(namespace["paw"].tools.mcp)
    assert "search" in dir(namespace["paw"].tools.mcp.mail)


def test_workspace_helpers_are_safe_and_hidden_from_user_state(
    tmp_path: Path,
) -> None:
    namespace, _ = build_namespace(RecordingChannel(), tmp_path, TOOLS)

    assert namespace["workspace"] == tmp_path.resolve()
    assert namespace["workspace_path"]("results/value.json") == (
        tmp_path / "results/value.json"
    )
    with pytest.raises(ValueError, match="relative"):
        namespace["workspace_path"]("/tmp/outside.json")
    internal = namespace["_paw_internal_names"]
    assert {"workspace", "workspace_path", "_paw_internal_names"} <= internal
