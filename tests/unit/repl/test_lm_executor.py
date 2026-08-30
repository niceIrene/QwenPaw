# -*- coding: utf-8 -*-
"""Main-process LMExecutor tests (design doc §3)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qwenpaw.repl import lm_executor
from qwenpaw.repl.lm_executor import (
    LMCallError,
    LMConfig,
    LMExecutor,
    config_from_env,
    lm_configured,
)


class FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=5):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, text, usage=None):
        if isinstance(text, list):
            self.content = text
        else:
            self.content = [{"type": "text", "text": text}]
        self.usage = usage or FakeUsage()


class FakeModel:
    """Async callable returning queued texts; records the messages it saw."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.raw_message_types = []

    async def __call__(self, messages=None, **kwargs):
        messages = list(messages or [])
        self.raw_message_types.append(
            [type(message).__name__ for message in messages],
        )
        self.calls.append(
            {"messages": [_message_as_dict(m) for m in messages], "kwargs": kwargs},
        )
        if not self.outputs:
            raise AssertionError("FakeModel ran out of queued outputs")
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return FakeResponse(output)


def _message_as_dict(message):
    if isinstance(message, dict):
        return message
    parts = []
    for block in getattr(message, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None:
            text = getattr(block, "output", None)
        if text is None and isinstance(block, dict):
            text = block.get("text") or block.get("output")
        if not isinstance(text, str):
            text = json.dumps(text, default=str) if text else ""
        parts.append(text)
    return {"role": getattr(message, "role", None), "content": "".join(parts)}


def make_executor(outputs, **config_overrides):
    config = LMConfig(
        provider_id="testprov",
        model="testmodel",
        **config_overrides,
    )
    model = FakeModel(outputs)
    executor = LMExecutor(config, model_factory=lambda override: model)
    return executor, model


def call_payload(task="do it", context=None, schema=None):
    return {
        "op": "call",
        "task": task,
        "context": context or [],
        "schema": schema,
        "evidence": False,
        "model": "small",
        "temperature": 0.0,
        "max_tokens": 512,
    }


# ------------------------------------------------------------- config/env
def test_config_from_env_defaults(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "myprov/mymodel")
    config = config_from_env()
    assert config.provider_id == "myprov"
    assert config.model == "mymodel"
    assert config.slot_override == "myprov:mymodel"
    assert config.item_bytes == 32 * 1024


def test_config_from_env_unset(monkeypatch):
    monkeypatch.delenv("QWENPAW_SMALL_MODEL", raising=False)
    assert config_from_env() is None
    assert lm_configured() is False


def test_config_from_env_rejects_bad_format(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "no-slash")
    with pytest.raises(LMCallError) as excinfo:
        config_from_env()
    assert excinfo.value.kind == "validation_error"


def test_default_executor_singleton(monkeypatch):
    monkeypatch.delenv("QWENPAW_SMALL_MODEL", raising=False)
    lm_executor.reset_default_lm_executor()
    assert lm_executor.get_default_lm_executor() is None
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "p/m")
    lm_executor.reset_default_lm_executor()
    assert lm_executor.get_default_lm_executor() is not None
    lm_executor.reset_default_lm_executor()


# ------------------------------------------------------------- happy path
async def test_call_ok_with_schema(tmp_path):
    executor, model = make_executor(['{"answer": "42"}'])
    result = await executor.execute(
        call_payload(
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["status"] == "ok"
    assert result["value"] == {"answer": "42"}
    assert result["usage"]["model"] == "testmodel"
    assert result["usage"]["input_tokens"] == 10
    messages = model.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "do it" in messages[1]["content"]
    # Regression: agentscope 2.0 ChatModelBase requires Msg objects;
    # passing plain dicts failed every real sub-LM call with TypeError.
    assert model.raw_message_types[0] == ["Msg", "Msg"]


async def test_call_marks_unknown_status(tmp_path):
    executor, _ = make_executor(['{"answer": "unknown"}'])
    result = await executor.execute(
        call_payload(
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["status"] == "unknown"


async def test_call_without_schema_returns_raw_text(tmp_path):
    executor, _ = make_executor(["just text"])
    result = await executor.execute(
        call_payload(schema=None),
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["value"] == "just text"


async def test_schema_repair_retries_once(tmp_path):
    executor, model = make_executor(["not json at all", '{"answer": "42"}'])
    result = await executor.execute(
        call_payload(
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["status"] == "ok"
    assert result["value"] == {"answer": "42"}
    assert len(model.calls) == 2
    repair_messages = model.calls[1]["messages"]
    assert repair_messages[-1]["role"] == "user"
    assert "failed output validation" in repair_messages[-1]["content"]


async def test_schema_invalid_after_failed_repair(tmp_path):
    executor, _ = make_executor(["garbage", "still garbage"])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            call_payload(
                schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            ),
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "schema_invalid"
    error = excinfo.value.as_error()
    assert error["kind"] == "schema_invalid"
    assert error["retryable"] is False


async def test_fenced_json_is_accepted(tmp_path):
    executor, _ = make_executor(['```json\n{"answer": "42"}\n```'])
    result = await executor.execute(
        call_payload(
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["value"] == {"answer": "42"}


# ----------------------------------------------------------------- budget
async def test_hard_call_budget_exhausts(tmp_path):
    executor, _ = make_executor(["text"] * 5, hard_calls=2)
    payload = call_payload()
    await executor.execute(payload, workspace=tmp_path, session_id="s1")
    await executor.execute(payload, workspace=tmp_path, session_id="s1")
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(payload, workspace=tmp_path, session_id="s1")
    assert excinfo.value.kind == "budget_exhausted"


async def test_soft_budget_note_attached_once(tmp_path):
    executor, _ = make_executor(["text"] * 3, soft_calls=1, hard_calls=10)
    payload = call_payload()
    first = await executor.execute(payload, workspace=tmp_path, session_id="s1")
    second = await executor.execute(payload, workspace=tmp_path, session_id="s1")
    assert "budget" in first.get("budget_note", "")
    assert "budget_note" not in second


async def test_budget_is_per_session(tmp_path):
    executor, _ = make_executor(["text"] * 5, hard_calls=1)
    payload = call_payload()
    await executor.execute(payload, workspace=tmp_path, session_id="s1")
    # A different session still has budget left.
    await executor.execute(payload, workspace=tmp_path, session_id="s2")
    with pytest.raises(LMCallError):
        await executor.execute(payload, workspace=tmp_path, session_id="s1")


# ---------------------------------------------------------------- context
async def test_file_ref_reads_line_range(tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    executor, model = make_executor(["ok"])
    await executor.execute(
        call_payload(
            context=[{"kind": "file", "path": "data.txt", "start": 2, "end": 3}],
        ),
        workspace=tmp_path,
        session_id="s1",
    )
    user_message = model.calls[0]["messages"][1]["content"]
    assert "l2\nl3" in user_message
    assert "l1" not in user_message


async def test_file_ref_missing_raises_dangling_ref(tmp_path):
    executor, _ = make_executor(["ok"])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            call_payload(context=[{"kind": "file", "path": "nope.txt"}]),
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "dangling_ref"


async def test_file_ref_traversal_rejected(tmp_path):
    executor, _ = make_executor(["ok"])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            call_payload(context=[{"kind": "file", "path": "../escape.txt"}]),
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "validation_error"


async def test_oversize_context_item_rejected(tmp_path):
    executor, _ = make_executor(["ok"], item_bytes=16)
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            call_payload(context=["x" * 64]),
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "context_too_large"


async def test_history_ref_reads_scroll_store(tmp_path):
    db_path = tmp_path / "history.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE conversation_history ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, kind TEXT, "
        "role TEXT, name TEXT, content TEXT)"
    )
    connection.execute(
        "INSERT INTO conversation_history "
        "(session_id, kind, role, name, content) VALUES (?, ?, ?, ?, ?)",
        ("s1", "turn", "user", "", "hello from s1"),
    )
    connection.execute(
        "INSERT INTO conversation_history "
        "(session_id, kind, role, name, content) VALUES (?, ?, ?, ?, ?)",
        ("s2", "turn", "user", "", "other session row"),
    )
    connection.commit()
    connection.close()

    executor, model = make_executor(["ok"])
    await executor.execute(
        call_payload(context=[{"kind": "history", "last_n": 5}]),
        workspace=tmp_path,
        session_id="s1",
    )
    user_message = model.calls[0]["messages"][1]["content"]
    assert "hello from s1" in user_message
    assert "other session row" not in user_message


async def test_history_ref_missing_store_fails(tmp_path):
    executor, _ = make_executor(["ok"])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            call_payload(context=[{"kind": "history"}]),
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.code == "history_unavailable"


# -------------------------------------------------------------------- map
async def test_map_preserves_order_and_items(tmp_path):
    executor, model = make_executor(['{"n": 0}', '{"n": 1}', '{"n": 2}'])
    results = await executor.execute(
        {
            "op": "map",
            "task": "classify",
            "items": ["a", "b", "c"],
            "context": [],
            "schema": {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            "on_error": "collect",
            "max_concurrency": 2,
        },
        workspace=tmp_path,
        session_id="s1",
    )
    assert [r["value"]["n"] for r in results] == [0, 1, 2]
    assert len(model.calls) == 3
    for call in model.calls:
        assert "## " in call["messages"][1]["content"]  # ITEM label section


async def test_map_collect_converts_item_errors(tmp_path):
    # a: bad output + bad repair (2 model calls); b: ok; c: no schema, plain.
    executor, _ = make_executor(["garbage", "still bad", '{"n": 2}', "plain"])
    results = await executor.execute(
        {
            "op": "map",
            "task": "classify",
            "items": [
                "a",
                {"payload": "b", "schema": {"type": "object", "required": ["n"]}},
                {"payload": "c", "schema": None},
            ],
            "context": [],
            "schema": {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            "on_error": "collect",
            "max_concurrency": 1,
        },
        workspace=tmp_path,
        session_id="s1",
    )
    assert results[0]["status"] == "error"
    assert results[0]["error"]["kind"] == "schema_invalid"
    assert results[1]["status"] == "ok"
    assert results[1]["value"] == {"n": 2}
    assert results[2]["status"] == "ok"
    assert results[2]["value"] == "plain"


async def test_map_fail_fast_raises(tmp_path):
    executor, _ = make_executor(["garbage", "garbage"], hard_calls=10)
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            {
                "op": "map",
                "task": "classify",
                "items": ["a"],
                "context": [],
                "schema": {"type": "object", "required": ["n"]},
                "on_error": "fail_fast",
                "max_concurrency": 1,
            },
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "schema_invalid"


# ------------------------------------------------------------------ misc
async def test_unknown_op_rejected(tmp_path):
    executor, _ = make_executor(["ok"])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            {"op": "teleport"},
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "validation_error"


async def test_unknown_model_tier_rejected(tmp_path):
    executor, _ = make_executor(["ok"])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            call_payload() | {"model": "huge"},
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "validation_error"


async def test_error_kinds_survive_make_error():
    error = LMCallError("dangling_ref", "missing").as_error()
    assert error["kind"] == "dangling_ref"
    error = LMCallError("context_too_large", "too big").as_error()
    assert error["kind"] == "context_too_large"
    error = LMCallError("lm_unavailable", "off").as_error()
    assert error["kind"] == "lm_unavailable"


# ------------------------------------------------------ item_schema gating
async def test_map_item_schema_rejects_bad_items_before_model_calls(tmp_path):
    executor, model = make_executor(['{"n": 1}'])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            {
                "op": "map",
                "task": "count",
                "items": [
                    {"task": "a"},
                    {"payload": "b"},
                    {"task": "c"},
                ],
                "item_schema": {"type": "object", "required": ["task"]},
            },
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "validation_error"
    assert "[1]" in str(excinfo.value)
    assert model.calls == []  # no tokens spent on a malformed batch


async def test_map_item_schema_passes_valid_items(tmp_path):
    executor, model = make_executor(['{"n": 1}', '{"n": 2}'])
    results = await executor.execute(
        {
            "op": "map",
            "task": "count",
            "items": [{"task": "a"}, {"task": "b"}],
            "item_schema": {"type": "object", "required": ["task"]},
            "item_schema_name": "with_task",
        },
        workspace=tmp_path,
        session_id="s1",
    )
    assert [r["status"] for r in results] == ["ok", "ok"]
    assert len(model.calls) == 2


async def test_call_result_carries_schema_name(tmp_path):
    executor, _ = make_executor(['{"answer": "42"}'])
    result = await executor.execute(
        {
            "op": "call",
            "task": "do it",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            },
            "schema_name": "answer_contract",
        },
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["schema_name"] == "answer_contract"


async def test_map_flows_schema_name_into_item_specs(tmp_path):
    executor, model = make_executor(['{"n": 1}'])
    results = await executor.execute(
        {
            "op": "map",
            "task": "count",
            "items": [{"payload": "a"}],
            "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
            "schema_name": "count_contract",
        },
        workspace=tmp_path,
        session_id="s1",
    )
    assert results[0]["schema_name"] == "count_contract"


# ------------------------------------------------------------ worker tools
def _tool_call_block(call_id, name, arguments):
    return {
        "type": "tool_call",
        "id": call_id,
        "name": name,
        "input": json.dumps(arguments),
    }


def test_config_from_env_parses_worker_tools(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "prov/model")
    monkeypatch.setenv("QWENPAW_LM_WORKER_TOOLS", "read_file, grep ,")
    config = config_from_env()
    assert config.worker_tools == ("read_file", "grep")
    assert config.tool_rounds == 8


def test_config_from_env_rejects_unknown_worker_tools(monkeypatch):
    monkeypatch.setenv("QWENPAW_SMALL_MODEL", "prov/model")
    monkeypatch.setenv("QWENPAW_LM_WORKER_TOOLS", "read_file,exec_shell")
    with pytest.raises(LMCallError) as excinfo:
        config_from_env()
    assert "exec_shell" in str(excinfo.value)


async def test_tools_requested_but_not_enabled(tmp_path):
    executor, model = make_executor(['{"n": 1}'])
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            {"op": "call", "task": "t", "tools": ["read_file"]},
            workspace=tmp_path,
            session_id="s1",
        )
    assert excinfo.value.kind == "validation_error"
    assert model.calls == []


async def test_tool_loop_reads_workspace_file(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n")
    executor, model = make_executor(
        [
            [_tool_call_block("c1", "read_file", {"path": "notes.txt"})],
            '{"found": "beta"}',
        ],
        worker_tools=("read_file",),
    )
    result = await executor.execute(
        {
            "op": "call",
            "task": "find the middle line",
            "tools": ["read_file"],
            "schema": {
                "type": "object",
                "properties": {"found": {"type": "string"}},
            },
        },
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["status"] == "ok"
    assert result["value"] == {"found": "beta"}
    assert len(model.calls) == 2
    # Round 2 must carry the tool result with the real file content.
    second_round = model.calls[1]["messages"]
    assert "beta" in second_round[-1]["content"]
    # Usage accumulates across tool-loop rounds.
    assert result["usage"]["input_tokens"] == 20
    # Tools were actually offered to the model.
    assert model.calls[0]["kwargs"]["tools"][0]["function"]["name"] == (
        "read_file"
    )


async def test_tool_loop_path_escape_returns_error_text(tmp_path):
    executor, model = make_executor(
        [
            [_tool_call_block("c1", "read_file", {"path": "../secret"})],
            "giving up",
        ],
        worker_tools=("read_file",),
    )
    result = await executor.execute(
        {"op": "call", "task": "t", "tools": ["read_file"]},
        workspace=tmp_path,
        session_id="s1",
    )
    assert result["status"] == "ok"
    assert result["value"] == "giving up"
    assert "[tool error]" in model.calls[1]["messages"][-1]["content"]


async def test_tool_loop_round_cap(tmp_path):
    infinite = [
        [_tool_call_block(f"c{i}", "list_dir", {"path": "."})]
        for i in range(5)
    ]
    executor, _ = make_executor(
        infinite,
        worker_tools=("list_dir",),
        tool_rounds=3,
    )
    with pytest.raises(LMCallError) as excinfo:
        await executor.execute(
            {"op": "call", "task": "t", "tools": ["list_dir"]},
            workspace=tmp_path,
            session_id="s1",
        )
    assert "tool rounds" in str(excinfo.value)


async def test_grep_and_list_dir_handlers(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.log").write_text("INFO ok\nERROR boom\n")
    (tmp_path / "b.log").write_text("ERROR twice\n")
    executor, _ = make_executor([], worker_tools=("grep", "list_dir"))
    listing = executor._execute_worker_tool("list_dir", {"path": "."}, tmp_path)
    assert "sub/" in listing and "b.log" in listing
    hits = executor._execute_worker_tool(
        "grep",
        {"pattern": "ERROR", "path": "."},
        tmp_path,
    )
    assert "sub/a.log:2: ERROR boom" in hits
    assert "b.log:1: ERROR twice" in hits
    assert "2 file(s) searched" in hits
