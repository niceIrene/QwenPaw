# -*- coding: utf-8 -*-
"""Tests for the self-contained BEAM Harbor judge."""

# pylint: disable=protected-access

from __future__ import annotations

from benchmark_adapters import beam_judge


def _fixtures():
    probing = {
        "information_extraction": [
            {
                "question": "What is the target?",
                "rubric": ["The target is 500 QPS"],
            },
        ],
        "summary": [
            {
                "question": "Summarize.",
                "rubric": ["Mentions indexing", "Mentions rollout"],
            },
        ],
    }
    answers = {
        "information_extraction": [
            {
                "id": "info-0",
                "question": "What is the target?",
                "llm_response": "500 QPS",
            },
        ],
        "summary": [
            {
                "id": "summary-0",
                "question": "Summarize.",
                "llm_response": "Indexing only",
            },
        ],
    }
    return probing, answers


def test_build_work_items_preserves_every_rubric():
    probing, answers = _fixtures()
    items = beam_judge.build_work_items(probing, answers)
    assert len(items) == 3
    assert [item["question_id"] for item in items] == [
        "info-0",
        "summary-0",
        "summary-0",
    ]


def test_score_answers_averages_rubrics_questions_and_categories(monkeypatch):
    probing, answers = _fixtures()

    def fake_judge(_config, _question, criterion, _response):
        score = 1.0 if criterion != "Mentions rollout" else 0.0
        return {"score": score, "reason": "test"}

    monkeypatch.setattr(beam_judge, "_judge_criterion", fake_judge)
    config = beam_judge.JudgeConfig("key", "https://judge.test/v1", "judge")
    report = beam_judge.score_answers(probing, answers, config)

    assert report["categories"]["information_extraction"]["score"] == 1.0
    assert report["categories"]["summary"]["score"] == 0.5
    assert report["overall"] == 0.75
    assert report["judge"]["variant"] == "beam-official-compatible-v1"


def test_parse_judgment_accepts_fenced_json():
    result = beam_judge._parse_judgment(
        '```json\n{"score": 0.5, "reason": "partial"}\n```',
    )
    assert result == {"score": 0.5, "reason": "partial"}


def test_event_ordering_metrics_uses_normalized_kendall_tau():
    rubrics = ["first", "second", "third"]
    lines = ["A", "B", "C"]

    perfect = beam_judge.event_ordering_metrics(
        rubrics,
        lines,
        [0, 1, 2],
    )
    reversed_order = beam_judge.event_ordering_metrics(
        rubrics,
        lines,
        [2, 1, 0],
    )

    assert perfect["tau_norm"] == 1.0
    assert reversed_order["tau_norm"] == 0.0
    assert perfect["f1"] == 1.0


def test_event_ordering_category_uses_tau_not_rubric_average(monkeypatch):
    probing = {
        "event_ordering": [
            {
                "question": "Put them in order.",
                "rubric": ["first", "second", "third"],
            },
        ],
    }
    answers = {
        "event_ordering": [
            {
                "id": "event-0",
                "question": "Put them in order.",
                "llm_response": "third\nsecond\nfirst",
            },
        ],
    }

    monkeypatch.setattr(
        beam_judge,
        "_judge_criterion",
        lambda *_args: {"score": 1.0, "reason": "all present"},
    )
    monkeypatch.setattr(
        beam_judge,
        "_judge_event_alignment",
        lambda *_args: [2, 1, 0],
    )
    config = beam_judge.JudgeConfig("key", "https://judge.test/v1", "judge")

    report = beam_judge.score_answers(probing, answers, config)

    question = report["questions"][0]
    assert question["llm_judge_score"] == 1.0
    assert question["score"] == 0.0
    assert report["overall"] == 0.0


def test_official_prompt_includes_question_and_scoring_contract():
    prompt = beam_judge._prompt("When?", "contains 14 days", "It took 14 days")

    assert "QUESTION (what the user asked): When?" in prompt
    assert "RUBRIC CRITERION (what to check): contains 14 days" in prompt
    assert "0.5 (Partial Compliance)" in prompt
