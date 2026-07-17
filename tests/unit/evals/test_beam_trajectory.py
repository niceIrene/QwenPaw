# -*- coding: utf-8 -*-
"""Tests for QwenPaw BEAM to Harbor ATIF conversion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark_adapters.qwenpaw_beam_trajectory import (
    build_beam_trajectory,
    write_beam_trajectory,
)


def _trace() -> dict:
    return {
        "probe": {
            "id": "information_extraction-0",
            "type": "information_extraction",
            "question": "What detection rate did I mention?",
        },
        "prompt": "Recall the history.\n\nWhat detection rate did I mention?",
        "answer": "The detection rate was 98%.",
        "elapsed_seconds": 12.5,
        "usage": {"input_tokens": 1200, "output_tokens": 80},
        "tool_steps": [
            {
                "name": "recall_history",
                "arguments": '{"op":"search","query":"detection rate"}',
                "output": "1 row: targeting 98% detection",
            },
            {
                "name": "recall_history",
                "arguments": "not-json",
                "output": "Input validation failed",
            },
        ],
        "events": [
            {
                "id": "reason-1",
                "object": "message",
                "type": "reasoning",
                "content": [{"text": "I should search the history."}],
            },
            {
                "id": "reason-1",
                "object": "message",
                "type": "reasoning",
                "content": [{"text": "I should search the history."}],
            },
            {
                "id": "answer-1",
                "object": "message",
                "type": "message",
                "usage": {"input_tokens": 1200, "output_tokens": 80},
            },
            {
                "id": "answer-1",
                "object": "message",
                "type": "message",
                "usage": {"input_tokens": 1200, "output_tokens": 80},
            },
        ],
    }


def test_build_beam_trajectory_preserves_probe_tools_and_metrics():
    trajectory = build_beam_trajectory(
        [_trace()],
        session_id="trial-agent",
        agent_version="2.0.0",
        model_name="dashscope/qwen3.7-max",
        totals={
            "input_tokens": 1200,
            "output_tokens": 80,
            "recall_calls": 2,
        },
        conversation_id="1",
    )

    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["session_id"] == "trial-agent"
    assert trajectory["agent"]["name"] == "qwenpaw-beam"
    assert [step["step_id"] for step in trajectory["steps"]] == [1, 2]
    assert [step["source"] for step in trajectory["steps"]] == [
        "user",
        "agent",
    ]

    user_step, agent_step = trajectory["steps"]
    assert user_step["message"].startswith("Recall the history")
    assert user_step["extra"]["probe_sessions_independent"] is True
    assert agent_step["message"] == "The detection rate was 98%."
    assert agent_step["reasoning_content"] == "I should search the history."
    assert agent_step["llm_call_count"] == 1
    assert agent_step["metrics"] == {
        "prompt_tokens": 1200,
        "completion_tokens": 80,
        "extra": {"elapsed_seconds": 12.5},
    }
    assert agent_step["tool_calls"][0]["arguments"] == {
        "op": "search",
        "query": "detection rate",
    }
    assert agent_step["tool_calls"][1]["arguments"] == {"raw": "not-json"}
    assert agent_step["observation"]["results"][0]["source_call_id"] == (
        agent_step["tool_calls"][0]["tool_call_id"]
    )
    assert trajectory["final_metrics"]["total_prompt_tokens"] == 1200
    assert trajectory["final_metrics"]["total_steps"] == 2
    assert trajectory["extra"]["probe_sessions_independent"] is True


def test_build_beam_trajectory_assigns_sequential_ids_across_probes():
    second = _trace()
    second["probe"] = {
        "id": "summarization-0",
        "type": "summarization",
        "question": "Summarize it.",
    }

    trajectory = build_beam_trajectory(
        [_trace(), second],
        session_id="trial-agent",
        agent_version="2.0.0",
        model_name="qwen3.7-max",
    )

    assert [step["step_id"] for step in trajectory["steps"]] == [1, 2, 3, 4]
    assert trajectory["final_metrics"]["extra"]["probes"] == 2


def test_build_beam_trajectory_requires_a_probe_trace():
    with pytest.raises(ValueError, match="at least one"):
        build_beam_trajectory(
            [],
            session_id="trial-agent",
            agent_version="2.0.0",
            model_name="qwen3.7-max",
        )


def test_write_beam_trajectory_backfills_existing_trial(tmp_path: Path):
    agent_dir = tmp_path / "trial" / "agent"
    trace_dir = agent_dir / "traces"
    trace_dir.mkdir(parents=True)
    metrics = {
        "conversation_id": "1",
        "model": "dashscope/qwen3.7-max",
        "probes": [{"id": "information_extraction-0"}],
        "totals": {
            "input_tokens": 1200,
            "output_tokens": 80,
            "recall_calls": 2,
        },
    }
    (agent_dir / "metrics.json").write_text(json.dumps(metrics))
    (trace_dir / "information_extraction-0.json").write_text(
        json.dumps(_trace()),
    )

    output = write_beam_trajectory(agent_dir, agent_version="2.0.0")

    trajectory = json.loads(output.read_text())
    assert output == agent_dir / "trajectory.json"
    assert trajectory["session_id"] == "trial"
    assert trajectory["agent"]["model_name"] == "dashscope/qwen3.7-max"
    assert len(trajectory["steps"]) == 2
