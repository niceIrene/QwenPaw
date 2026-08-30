# -*- coding: utf-8 -*-
"""Kernel-side paw.lm proxy tests (design doc §2.2, §3.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw.repl.lm_runtime import (
    INLINE_ITEM_BYTES,
    LMNamespace,
    PawLMError,
    TaskResult,
    VAR_SPILL_BYTES,
)


class FakeChannel:
    """Capture lm_call payloads and return canned results."""

    def __init__(self, results=None):
        self.payloads = []
        self.results = list(results or [])

    def call_lm(self, payload):
        self.payloads.append(payload)
        if self.results:
            return self.results.pop(0)
        if payload["op"] == "map":
            return [
                {"status": "ok", "value": {"n": i}, "raw": "", "usage": {}}
                for i, _ in enumerate(payload["items"])
            ]
        return {"status": "ok", "value": {"answer": "42"}, "raw": "", "usage": {}}


def make_lm(namespace=None, results=None, workspace=None):
    ns = namespace if namespace is not None else {}
    channel = FakeChannel(results)
    lm = LMNamespace(channel, ns, workspace or Path("/tmp/paw-lm-test"))
    return lm, channel


def test_task_result_attribute_access_falls_through_to_value():
    result = TaskResult(
        {"status": "ok", "value": {"passed": True, "violations": []}}
    )
    assert result.status == "ok"
    assert result["status"] == "ok"
    assert result.passed is True
    with pytest.raises(AttributeError):
        result.missing_key


def test_call_builds_payload_and_returns_task_result():
    lm, channel = make_lm()
    result = lm.call("What is the answer?", context=["some context"])
    assert result.value == {"answer": "42"}
    payload = channel.payloads[0]
    assert payload["op"] == "call"
    assert payload["task"] == "What is the answer?"
    assert payload["context"] == [{"kind": "inline", "text": "some context"}]
    assert payload["schema"] is None
    assert payload["model"] == "small"


def test_call_requires_non_empty_task():
    lm, _ = make_lm()
    with pytest.raises(PawLMError) as excinfo:
        lm.call("   ")
    assert excinfo.value.kind == "validation_error"


def test_var_ref_resolves_from_namespace():
    lm, channel = make_lm(namespace={"table": [1, 2, 3]})
    lm.call("count rows", context=[lm.var("table")])
    item = channel.payloads[0]["context"][0]
    assert item["kind"] == "var"
    assert item["name"] == "table"
    assert json.loads(item["value"]) == [1, 2, 3]
    assert item["label"] == "variable table"


def test_var_ref_dangling_raises():
    lm, _ = make_lm()
    with pytest.raises(PawLMError) as excinfo:
        lm.call("count rows", context=[lm.var("nope")])
    assert excinfo.value.kind == "dangling_ref"


def test_var_ref_spills_oversize_to_file(tmp_path):
    big = "x" * (VAR_SPILL_BYTES + 10)
    lm, channel = make_lm(namespace={"big": big}, workspace=tmp_path)
    lm.call("inspect", context=[lm.var("big")])
    item = channel.payloads[0]["context"][0]
    assert item["kind"] == "file"
    assert item["max_bytes"] == VAR_SPILL_BYTES
    assert "spilled" in item["note"]
    spilled = tmp_path / item["path"]
    assert spilled.is_file()
    assert spilled.read_text(encoding="utf-8") == big


def test_inline_text_over_cap_raises(tmp_path):
    lm, _ = make_lm(workspace=tmp_path)
    with pytest.raises(PawLMError) as excinfo:
        lm.call("read", context=["y" * (INLINE_ITEM_BYTES + 1)])
    assert excinfo.value.kind == "context_too_large"


def test_file_and_history_refs_pass_through():
    lm, channel = make_lm()
    lm.call(
        "inspect",
        context=[
            lm.file("logs/app.log", start=10, end=20, max_bytes=1024),
            lm.history(last_n=5, query="error"),
        ],
    )
    context = channel.payloads[0]["context"]
    assert context[0] == {
        "kind": "file",
        "path": "logs/app.log",
        "start": 10,
        "end": 20,
        "max_bytes": 1024,
    }
    assert context[1] == {"kind": "history", "last_n": 5, "query": "error"}


def test_unknown_context_kind_raises():
    lm, _ = make_lm()
    with pytest.raises(PawLMError) as excinfo:
        lm.call("x", context=[{"kind": "url", "href": "https://x"}])
    assert excinfo.value.kind == "validation_error"


def test_map_bare_items_become_payloads():
    lm, channel = make_lm()
    results = lm.map("classify", items=["a", "b", "c"])
    assert [r.value["n"] for r in results] == [0, 1, 2]
    payload = channel.payloads[0]
    assert payload["op"] == "map"
    assert payload["items"] == [
        {"payload": "a"},
        {"payload": "b"},
        {"payload": "c"},
    ]
    assert payload["on_error"] == "collect"


def test_map_item_overrides_resolve_context():
    lm, channel = make_lm(namespace={"rows": [1]})
    lm.map(
        items=[
            {
                "task": "special",
                "context": [lm.var("rows")],
                "schema": {"type": "object"},
            },
        ],
    )
    item = channel.payloads[0]["items"][0]
    assert item["task"] == "special"
    assert item["context"][0]["kind"] == "var"
    assert item["schema"] == {"type": "object"}


def test_map_rejects_bad_on_error():
    lm, _ = make_lm()
    with pytest.raises(PawLMError):
        lm.map("t", items=["a"], on_error="ignore")


def test_extract_defaults_to_answer_schema():
    lm, channel = make_lm()
    assert lm.extract("the answer?", context=["ctx"]) == "42"
    payload = channel.payloads[0]
    assert payload["schema"]["required"] == ["answer"]


def test_summarize_returns_plain_text():
    lm, channel = make_lm(results=[{"status": "ok", "value": "short summary"}])
    assert lm.summarize(context=["long text"], focus="errors") == "short summary"
    payload = channel.payloads[0]
    assert payload["schema"] is None
    assert "errors" in payload["task"]


def test_verify_uses_fixed_schema_and_labels_artifact():
    lm, channel = make_lm(
        results=[
            {
                "status": "ok",
                "value": {"passed": False, "violations": ["x"], "evidence": []},
            },
        ]
    )
    result = lm.verify("the artifact", "must do x")
    assert result.passed is False
    assert result.value["violations"] == ["x"]
    payload = channel.payloads[0]
    assert payload["schema"]["required"] == ["passed", "violations", "evidence"]
    assert payload["context"][0]["label"] == "ARTIFACT"
    assert payload["context"][0]["text"] == "the artifact"


def test_verify_rejects_unexpected_kwargs():
    lm, _ = make_lm()
    with pytest.raises(PawLMError) as excinfo:
        lm.verify("a", "s", bogus=1)
    assert excinfo.value.kind == "validation_error"


# ---------------------------------------------------------- named contracts
def test_schema_registers_and_resolves_by_name():
    lm, channel = make_lm()
    definition = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    assert lm.schema("count", definition) == {
        "name": "count",
        "registered": True,
    }
    lm.call("count things", schema="count")
    payload = channel.payloads[0]
    assert payload["schema"] == definition
    assert payload["schema_name"] == "count"


def test_schema_unknown_name_raises_validation_error():
    lm, _ = make_lm()
    lm.schema("known", {"type": "object"})
    with pytest.raises(PawLMError) as excinfo:
        lm.call("t", schema="missing")
    assert excinfo.value.kind == "validation_error"
    assert "known" in str(excinfo.value)


def test_schema_rejects_bad_arguments():
    lm, _ = make_lm()
    with pytest.raises(PawLMError):
        lm.schema("", {"type": "object"})
    with pytest.raises(PawLMError):
        lm.schema("x", {})
    with pytest.raises(PawLMError):
        lm.call("t", schema=42)


def test_map_resolves_named_schemas_for_shared_and_per_item():
    lm, channel = make_lm()
    lm.schema("shared", {"type": "object"})
    lm.schema("per_item", {"type": "array"})
    lm.map(
        "t",
        items=[{"payload": "a"}, {"payload": "b", "schema": "per_item"}],
        schema="shared",
        item_schema={"type": "object", "required": ["payload"]},
    )
    payload = channel.payloads[0]
    assert payload["schema"] == {"type": "object"}
    assert payload["schema_name"] == "shared"
    assert payload["item_schema"] == {
        "type": "object",
        "required": ["payload"],
    }
    assert payload["items"][1]["schema"] == {"type": "array"}
    assert payload["items"][1]["schema_name"] == "per_item"
    assert "schema" not in payload["items"][0]


def test_map_item_schema_accepts_registered_name():
    lm, channel = make_lm()
    lm.schema("item_contract", {"type": "object", "required": ["task"]})
    lm.map(items=[{"task": "x"}], item_schema="item_contract")
    payload = channel.payloads[0]
    assert payload["item_schema"] == {
        "type": "object",
        "required": ["task"],
    }
    assert payload["item_schema_name"] == "item_contract"


def test_call_and_map_forward_tools():
    lm, channel = make_lm()
    lm.call("t", tools=["read_file"])
    assert channel.payloads[0]["tools"] == ["read_file"]
    lm.map(items=["a"], tools=["grep", "read_file"])
    assert channel.payloads[1]["tools"] == ["grep", "read_file"]
    lm.call("t2")
    assert channel.payloads[2]["tools"] is None
