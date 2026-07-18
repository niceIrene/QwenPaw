# -*- coding: utf-8 -*-
"""Unit tests for the BEAM benchmark runner."""

# pylint: disable=protected-access

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.evals import beam_runner


def _chat() -> list[dict]:
    return [
        {
            "batch_number": 1,
            "turns": [
                [
                    {
                        "role": "user",
                        "id": 10,
                        "time_anchor": "July-01-2024",
                        "index": "1,1",
                        "question_type": "main_question",
                        "content": "My deployment starts on Friday.",
                    },
                    {
                        "role": "assistant",
                        "id": 11,
                        "content": "Understood.",
                    },
                ],
            ],
        },
        {
            "batch_number": 2,
            "turns": [
                [
                    {
                        "role": "user",
                        "id": 12,
                        "time_anchor": "July-02-2024",
                        "index": "2,1",
                        "content": "The target is 500 queries per second. 你好",
                    },
                ],
            ],
        },
    ]


def test_iter_json_array_streams_small_chunks(tmp_path: Path, monkeypatch):
    source = tmp_path / "chat.json"
    source.write_text(
        json.dumps(_chat(), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(beam_runner, "_CHUNK_SIZE", 7)

    loaded = list(beam_runner.iter_json_array(source))

    assert loaded == _chat()


@pytest.mark.parametrize("payload", ["", "{}", "[{"])
def test_iter_json_array_rejects_invalid_input(tmp_path: Path, payload: str):
    source = tmp_path / "invalid.json"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError):
        list(beam_runner.iter_json_array(source))


def test_load_questions_validates_and_limits(tmp_path: Path):
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps(
            [
                {"id": "q0", "type": "temporal", "question": "When?"},
                {"id": "q1", "type": "summary", "question": "Summarize."},
            ],
        ),
        encoding="utf-8",
    )

    assert beam_runner.load_questions(questions, limit=1) == [
        {"id": "q0", "type": "temporal", "question": "When?"},
    ]


def test_load_questions_rejects_duplicate_ids(tmp_path: Path):
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps(
            [
                {"id": "q0", "type": "a", "question": "One?"},
                {"id": "q0", "type": "b", "question": "Two?"},
            ],
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        beam_runner.load_questions(questions)


def test_extract_answer_keeps_only_final_message_and_strips_headline():
    events = [
        SimpleNamespace(
            object="message",
            id="progress-1",
            type="message",
            role="assistant",
            status="completed",
            content=[SimpleNamespace(text="Let me search the history.\n\n")],
        ),
        SimpleNamespace(
            object="message",
            id="reasoning-1",
            type="reasoning",
            role="assistant",
            status="completed",
            content=[SimpleNamespace(text="Internal reasoning")],
        ),
        SimpleNamespace(
            object="message",
            id="answer-1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                {"type": "text", "text": "The final answer is 45 days.\n\n"},
                SimpleNamespace(
                    text="<!-- ⟦ The dates are 45 days apart ⟧ -->",
                ),
            ],
        ),
    ]

    answer = beam_runner._extract_answer(events)

    assert answer == "The final answer is 45 days."
    assert "Let me search" not in answer
    assert "⟦" not in answer


def test_task_dir_resolves_harbor_environment_inputs(tmp_path: Path):
    environment = tmp_path / "environment"
    environment.mkdir()
    chat = environment / "chat.json"
    questions = environment / "questions.json"
    chat.write_text("[]", encoding="utf-8")
    questions.write_text("[]", encoding="utf-8")
    args = SimpleNamespace(
        task_dir=str(tmp_path),
        chat=None,
        questions=None,
        out=None,
    )

    resolved_chat, resolved_questions, out = beam_runner._resolve_paths(args)

    assert resolved_chat == chat.resolve()
    assert resolved_questions == questions.resolve()
    assert out == (tmp_path / "answers.json").resolve()


def test_ingest_chat_preserves_sessions_metadata_and_dates(tmp_path: Path):
    chat = tmp_path / "chat.json"
    chat.write_text(json.dumps(_chat()), encoding="utf-8")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = SimpleNamespace(
        workspace_dir=workspace_dir,
        agent_id="beam-agent",
    )

    stats = beam_runner.ingest_chat(
        workspace,
        chat,
        conversation_id="10M-1",
        replace_history=False,
    )

    assert stats.sessions == 2
    assert stats.messages == 3
    assert stats.rows == 3
    connection = sqlite3.connect(workspace_dir / "history.db")
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT session_id, agent_id, kind, role, content, metadata, "
        "created_at "
        "FROM conversation_history ORDER BY seq",
    ).fetchall()
    connection.close()

    assert [row["session_id"] for row in rows] == [
        "beam__10M-1__batch_001",
        "beam__10M-1__batch_001",
        "beam__10M-1__batch_002",
    ]
    assert {row["kind"] for row in rows} == {beam_runner.BEAM_HISTORY_KIND}
    assert {row["agent_id"] for row in rows} == {"beam-agent"}
    assert rows[0]["content"] == "My deployment starts on Friday."
    assert rows[0]["created_at"].startswith("2024-07-01T00:00:00")
    assert rows[2]["created_at"].startswith("2024-07-02T00:00:00")
    metadata = json.loads(rows[1]["metadata"])
    assert metadata["batch_number"] == 1
    assert metadata["time_anchor"] == "July-01-2024"


def test_ingest_chat_refuses_to_destroy_existing_history(tmp_path: Path):
    chat = tmp_path / "chat.json"
    chat.write_text(json.dumps(_chat()), encoding="utf-8")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "history.db").write_bytes(b"existing")
    workspace = SimpleNamespace(workspace_dir=workspace_dir, agent_id="beam")

    with pytest.raises(RuntimeError, match="Refusing to replace"):
        beam_runner.ingest_chat(
            workspace,
            chat,
            conversation_id="10M-1",
            replace_history=False,
        )

    assert (workspace_dir / "history.db").read_bytes() == b"existing"


def test_configure_benchmark_history_disables_retention(monkeypatch):
    scroll_config = SimpleNamespace(history_retention_days=30)
    agent_config = SimpleNamespace(
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                scroll_config=scroll_config,
            ),
        ),
    )
    saved = []
    monkeypatch.setattr(
        "qwenpaw.config.config.load_agent_config",
        lambda agent_id: agent_config,
    )
    monkeypatch.setattr(
        "qwenpaw.config.config.save_agent_config",
        lambda agent_id, config: saved.append((agent_id, config)),
    )

    beam_runner._configure_benchmark_history("beam-agent")

    assert scroll_config.history_retention_days == 0
    assert saved == [("beam-agent", agent_config)]


class _FakeWorkspace:
    def __init__(self, events):
        self.events = events
        self.request = None

    async def stream_query(self, request):
        self.request = request
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_ask_probe_writes_answer_metrics_and_trace(tmp_path: Path):
    events = [
        SimpleNamespace(
            object="message",
            type="plugin_call",
            content=[
                SimpleNamespace(
                    data={
                        "call_id": "call-1",
                        "name": "recall_history",
                        "arguments": {"op": "search", "query": "target"},
                    },
                ),
            ],
        ),
        SimpleNamespace(
            object="message",
            type="plugin_call_output",
            content=[
                SimpleNamespace(
                    data={
                        "call_id": "call-1",
                        "name": "recall_history",
                        "output": "500 queries per second",
                    },
                ),
            ],
        ),
        SimpleNamespace(
            object="content",
            type="text",
            delta=False,
            msg_id="answer-1",
            text="The target is 500 queries per second.",
        ),
        SimpleNamespace(
            object="message",
            id="answer-1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                SimpleNamespace(
                    text="The target is 500 queries per second.",
                ),
            ],
            usage={"input_tokens": 120, "output_tokens": 10},
        ),
    ]
    workspace = _FakeWorkspace(events)
    probe = {"id": "info-0", "type": "information", "question": "Target?"}

    answer, metrics = await beam_runner.ask_probe(
        workspace,
        agent_id="beam-agent",
        conversation_id="10M-1",
        probe=probe,
        recall_tool="structured",
        trace_dir=tmp_path,
    )

    assert answer == "The target is 500 queries per second."
    assert metrics["recall_calls"] == 1
    assert metrics["input_tokens"] == 120
    assert metrics["output_tokens"] == 10
    assert workspace.request["session_id"].endswith("probe__info-0")
    prompt = workspace.request["input"][0]["content"][0]["text"]
    assert "kind='beam_chat_turn'" in prompt
    assert "lo and hi as unquoted JSON integers" in prompt
    assert "complete user-bounded turn" in prompt
    assert "turn_start_seq through turn_end_seq" in prompt
    assert "created_on='YYYY-MM-DD'" in prompt
    assert "created_from and created_to" in prompt
    assert "op='days_between'" in prompt
    assert "search each one separately" in prompt
    assert "Preserve exact numbers, units, and version labels" in prompt
    assert "same project and the same fact" in prompt
    assert "Start with k=5 or k=10" in prompt
    assert "complete user-bounded turn" in beam_runner._PYTHON_PROMPT
    assert "turn_start_seq and turn_end_seq" in beam_runner._PYTHON_PROMPT
    assert "ms.days_between(start, end)" in beam_runner._PYTHON_PROMPT
    assert "search each one separately" in beam_runner._PYTHON_PROMPT
    assert "Preserve exact numbers, units, and version labels" in (
        beam_runner._PYTHON_PROMPT
    )
    trace = json.loads((tmp_path / "info-0.json").read_text())
    assert trace["tool_steps"][0]["name"] == "recall_history"
    assert trace["answer"] == answer
