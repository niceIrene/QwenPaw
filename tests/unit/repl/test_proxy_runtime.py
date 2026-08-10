"""Kernel proxy discovery, signatures, and safe-save tests."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from qwenpaw.repl.proxy_runtime import build_namespace, compact_peek


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


def test_peek_recursively_bounds_nested_tool_results() -> None:
    courses = [
        {
            "id": index,
            "name": "course-" + ("x" * 500),
            "description": "y" * 500,
            "ignored": "z" * 500,
        }
        for index in range(10)
    ]

    preview_text = compact_peek(courses)
    preview = json.loads(preview_text)

    assert preview["type"] == "list"
    assert preview["len"] == 10
    assert len(preview["sample"]) == 3
    assert list(preview["sample"][0]) == ["id", "name", "description"]
    assert len(preview["sample"][0]["name"]) <= 120
    assert len(preview_text.encode("utf-8")) < 1500


def test_peek_accepts_a_bounded_positional_item_limit() -> None:
    preview = json.loads(compact_peek(list(range(20)), 1))

    assert preview["len"] == 20
    assert preview["sample"] == [0]
    with pytest.raises(ValueError, match="between 1 and 10"):
        compact_peek(list(range(20)), 11)


def test_save_stays_inside_workspace(tmp_path: Path) -> None:
    namespace, _ = build_namespace(RecordingChannel(), tmp_path, TOOLS)
    assert namespace["save"]({"value": 1}, "results/value.json") == (
        "results/value.json"
    )
    assert (tmp_path / "results/value.json").is_file()

    with pytest.raises(ValueError, match="relative"):
        namespace["save"]("secret", "../escape.txt")


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
    assert "workspace" not in {item["name"] for item in namespace["ls_vars"]()}


def test_peek_file_is_bounded_and_workspace_safe(tmp_path: Path) -> None:
    namespace, _ = build_namespace(RecordingChannel(), tmp_path, TOOLS)
    source = tmp_path / "out" / "large.txt"
    source.parent.mkdir()
    source.write_text("abcdefghij" * 300, encoding="utf-8")

    preview = json.loads(
        namespace["peek_file"]("out/large.txt", max_bytes=12, offset=5),
    )

    assert preview == {
        "path": "out/large.txt",
        "offset": 5,
        "total_bytes": 3000,
        "sample": "fghijabcdefg",
        "has_more": True,
    }
    compat_preview = json.loads(
        namespace["peek_file"]("out/large.txt", max_chars=8000),
    )
    assert len(compat_preview["sample"]) == 2000
    assert compat_preview["has_more"] is True
    with pytest.raises(ValueError, match="relative"):
        namespace["peek_file"]("../outside.txt")
    with pytest.raises(ValueError, match="between 1 and 2000"):
        namespace["peek_file"]("out/large.txt", max_bytes=2001)
    with pytest.raises(ValueError, match="only one"):
        namespace["peek_file"](
            "out/large.txt",
            max_bytes=12,
            max_chars=12,
        )
    assert "peek_file" not in {item["name"] for item in namespace["ls_vars"]()}
