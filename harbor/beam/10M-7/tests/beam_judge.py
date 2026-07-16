# -*- coding: utf-8 -*-
"""Official-compatible BEAM judge packaged for Harbor.

The upstream evaluator uses the unified BEAM rubric prompt for nine abilities.
For event ordering it semantically aligns response lines to rubric events and
reports normalized Kendall tau instead of the rubric score.  This lightweight
port preserves those scoring semantics without the upstream NLTK, SciPy, and
sentence-transformers dependencies.  Event alignment is batched into one LLM
call per question rather than the upstream pairwise calls.
"""

# The official judge prompt intentionally keeps one instruction per physical
# line. Reflowing it changes the exact prompt used for benchmark comparison.
# flake8: noqa: E501
# pylint: disable=line-too-long

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JUDGE_VARIANT = "beam-official-compatible-v1"
OFFICIAL_BEAM_COMMIT = "3e12035532eb85768f1a7cd779832b650c4b2ef9"
OFFICIAL_SOURCE = (
    "https://github.com/mohammadtavakoli78/BEAM/tree/main/src/evaluation"
)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class JudgeConfig:
    api_key: str
    base_url: str
    model: str
    concurrency: int = 4
    retries: int = 4
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "JudgeConfig":
        explicit_key = os.getenv("BEAM_JUDGE_API_KEY", "").strip()
        openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        dashscope_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        api_key = explicit_key or openai_key or dashscope_key
        if not api_key:
            raise RuntimeError(
                "BEAM judge API key missing. Set BEAM_JUDGE_API_KEY, "
                "OPENAI_API_KEY, or DASHSCOPE_API_KEY.",
            )
        base_url = os.getenv("BEAM_JUDGE_BASE_URL", "").strip()
        if not base_url:
            base_url = (
                "https://dashscope.aliyuncs.com/compatible-mode/v1"
                if dashscope_key and not openai_key and not explicit_key
                else "https://api.openai.com/v1"
            )
        model = os.getenv("BEAM_JUDGE_MODEL", "").strip()
        if not model:
            model = (
                "qwen3.7-max"
                if "dashscope.aliyuncs.com" in base_url
                else "gpt-4.1-mini"
            )
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            concurrency=max(1, int(os.getenv("BEAM_JUDGE_CONCURRENCY", "4"))),
        )


def _prompt(question: str, criterion: str, response: str) -> str:
    """Render BEAM's official unified LLM-judge prompt."""

    return f"""You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- QUESTION (what the user asked): {question}
- RUBRIC CRITERION (what to check): {criterion}
- RESPONSE TO EVALUATE: {response}

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate.

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT (anchored to the QUESTION)
A compliant response must be **on-topic with respect to the QUESTION** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms. Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** over string matching.

## STYLE NEUTRALITY:
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format or structure.
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
- **0.5 (Partial Compliance)**: Partially complies or has minor inaccuracies/incomplete execution.
- **0.0 (No Compliance)**: Required content is missing/incorrect, a negative constraint is violated, or the response is non-responsive.

## EVALUATION INSTRUCTIONS:
1. Determine whether the criterion is positive or negative.
2. For compound criteria, require all elements for 1.0, some for 0.5, and none for 0.0.
3. Check semantic compliance.
4. Assign a score.
5. Explain the score.

## OUTPUT FORMAT:
Return only this JSON object:
{{"score": 1.0, "reason": "detailed justification"}}"""


def _parse_judgment(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        match = _JSON_RE.search(cleaned)
        if not match:
            raise ValueError(
                f"judge returned no JSON object: {text[:200]!r}",
            ) from exc
        data = json.loads(match.group(0))
    score = float(data["score"])
    if score not in (0.0, 0.5, 1.0):
        raise ValueError(f"judge score must be 0, 0.5, or 1; got {score}")
    return {"score": score, "reason": str(data.get("reason") or "")}


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def _chat_completion(
    config: JudgeConfig,
    prompt: str,
    *,
    max_tokens: int,
) -> str:
    payload = json.dumps(
        {
            "model": config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
    ).encode("utf-8")
    delay = 2.0
    for attempt in range(config.retries):
        request = urllib.request.Request(
            _chat_completions_url(config.base_url),
            data=payload,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=config.timeout_seconds,
            ) as response_obj:
                body = json.loads(response_obj.read().decode("utf-8"))
            return str(body["choices"][0]["message"]["content"])
        except (
            urllib.error.URLError,
            TimeoutError,
            KeyError,
            IndexError,
            ValueError,
        ):
            if attempt + 1 >= config.retries:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def _judge_criterion(
    config: JudgeConfig,
    question: str,
    criterion: str,
    response: str,
) -> dict[str, Any]:
    content = _chat_completion(
        config,
        _prompt(question, criterion, response),
        max_tokens=350,
    )
    return _parse_judgment(content)


def _event_alignment_prompt(
    question: str,
    rubrics: list[str],
    response_lines: list[str],
) -> str:
    rubric_rows = "\n".join(
        f"{index}: {rubric}" for index, rubric in enumerate(rubrics)
    )
    response_rows = "\n".join(
        f"{index}: {line}" for index, line in enumerate(response_lines)
    )
    return f"""You are reproducing BEAM's semantic event alignment step.

Question:
{question}

Canonical events, in gold order:
{rubric_rows}

Response lines, in predicted order:
{response_rows}

For every response line, identify the single canonical event that describes
the same event or fact. Paraphrases count as equivalent. Use null for a line
that does not match. A canonical index may be used at most once. Do not reorder
the response lines.

Return only JSON with an array of exactly {len(response_lines)} elements:
{{"line_to_rubric_index": [0, null, 2]}}"""


def _parse_event_alignment(text: str, line_count: int) -> list[int | None]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        match = _JSON_RE.search(cleaned)
        if not match:
            raise ValueError(
                "event alignment returned no JSON object",
            ) from exc
        data = json.loads(match.group(0))
    raw = data.get("line_to_rubric_index")
    if not isinstance(raw, list) or len(raw) != line_count:
        raise ValueError(
            "event alignment must return one mapping per response line",
        )
    result: list[int | None] = []
    seen: set[int] = set()
    for value in raw:
        if value is None:
            result.append(None)
            continue
        if isinstance(value, bool):
            raise ValueError("event alignment indices must be integers")
        index = int(value)
        if index < 0 or index in seen:
            result.append(None)
            continue
        seen.add(index)
        result.append(index)
    return result


def _judge_event_alignment(
    config: JudgeConfig,
    question: str,
    rubrics: list[str],
    response_lines: list[str],
) -> list[int | None]:
    content = _chat_completion(
        config,
        _event_alignment_prompt(question, rubrics, response_lines),
        max_tokens=max(300, len(response_lines) * 12),
    )
    return _parse_event_alignment(content, len(response_lines))


def _kendall_tau_b(left: list[int], right: list[int]) -> float:
    """Compute Kendall tau-b with ties using only the standard library."""

    if len(left) != len(right):
        raise ValueError("rank vectors must have the same length")
    concordant = discordant = ties_left = ties_right = 0
    for first, left_value in enumerate(left):
        for second in range(first + 1, len(left)):
            delta_left = left_value - left[second]
            delta_right = right[first] - right[second]
            if delta_left == 0 and delta_right == 0:
                continue
            if delta_left == 0:
                ties_left += 1
            elif delta_right == 0:
                ties_right += 1
            elif delta_left * delta_right > 0:
                concordant += 1
            else:
                discordant += 1
    numerator = concordant - discordant
    denominator = (
        (concordant + discordant + ties_left)
        * (concordant + discordant + ties_right)
    ) ** 0.5
    return numerator / denominator if denominator else 0.0


def event_ordering_metrics(
    rubrics: list[str],
    response_lines: list[str],
    mapping: list[int | None],
) -> dict[str, Any]:
    """Apply the official BEAM event-ordering aggregation after alignment."""

    reference = [f"rubric:{index}" for index in range(len(rubrics))]
    system: list[str] = []
    for line_index, mapped in enumerate(mapping):
        if mapped is not None and mapped < len(reference):
            system.append(reference[mapped])
        else:
            system.append(f"extra:{line_index}:{response_lines[line_index]}")

    reference_set = set(reference)
    true_positives = len(reference_set & set(system))
    false_positives = sum(item not in reference_set for item in system)
    false_negatives = sum(item not in system for item in reference)
    precision = (
        true_positives / (true_positives + false_positives)
        if true_positives + false_positives
        else 0.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if true_positives + false_negatives
        else 0.0
    )
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    union = list(dict.fromkeys(reference + system))
    tie_rank = len(union) + 1

    def ranks(sequence: list[str]) -> list[int]:
        positions = {item: index + 1 for index, item in enumerate(sequence)}
        return [positions.get(item, tie_rank) for item in union]

    tau = _kendall_tau_b(ranks(reference), ranks(system))
    tau_normalized = (tau + 1) / 2
    return {
        "response_lines": response_lines,
        "line_to_rubric_index": mapping,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tau_norm": tau_normalized,
        "final_score": tau_normalized * f1,
    }


def _load_json_object(path: str | Path, label: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return data


def build_work_items(
    probing_questions: dict[str, Any],
    answers: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if set(answers) != set(probing_questions):
        missing = sorted(set(probing_questions) - set(answers))
        extra = sorted(set(answers) - set(probing_questions))
        raise ValueError(
            f"answer categories do not match probes; missing={missing}, extra={extra}",
        )
    for category, probes in probing_questions.items():
        category_answers = answers.get(category)
        if not isinstance(probes, list) or not isinstance(
            category_answers,
            list,
        ):
            raise ValueError(f"BEAM category {category!r} must be an array")
        if len(probes) != len(category_answers):
            raise ValueError(
                f"BEAM category {category!r} has {len(probes)} probes but "
                f"{len(category_answers)} answers",
            )
        for index, (probe, answer) in enumerate(zip(probes, category_answers)):
            question = str(probe.get("question") or "").strip()
            answer_question = str(answer.get("question") or "").strip()
            if not question or question != answer_question:
                raise ValueError(
                    f"question mismatch in {category}[{index}]",
                )
            response = str(answer.get("llm_response") or "")
            rubrics = probe.get("rubric")
            if not isinstance(rubrics, list) or not rubrics:
                raise ValueError(f"missing rubric in {category}[{index}]")
            question_id = str(answer.get("id") or f"{category}-{index}")
            for rubric_index, criterion in enumerate(rubrics):
                items.append(
                    {
                        "category": category,
                        "question_id": question_id,
                        "question": question,
                        "response": response,
                        "rubric_index": rubric_index,
                        "criterion": str(criterion),
                    },
                )
    return items


def score_answers(
    probing_questions: dict[str, Any],
    answers: dict[str, Any],
    config: JudgeConfig,
) -> dict[str, Any]:
    items = build_work_items(probing_questions, answers)

    def judge(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        result = _judge_criterion(
            config,
            item["question"],
            item["criterion"],
            item["response"],
        )
        return item, result

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.concurrency,
    ) as executor:
        judged = list(executor.map(judge, items))

    questions: dict[str, dict[str, Any]] = {}
    for item, judgment in judged:
        key = f"{item['category']}::{item['question_id']}"
        question = questions.setdefault(
            key,
            {
                "id": item["question_id"],
                "category": item["category"],
                "question": item["question"],
                "response": item["response"],
                "criteria": [],
            },
        )
        question["criteria"].append(
            {
                "rubric_index": item["rubric_index"],
                "criterion": item["criterion"],
                **judgment,
            },
        )

    event_questions = [
        question
        for question in questions.values()
        if question["category"] == "event_ordering"
    ]

    def align_event(question: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        rubrics = [item["criterion"] for item in question["criteria"]]
        response_lines = [
            line.strip()
            for line in question["response"].splitlines()
            if line.strip()
        ]
        mapping = (
            _judge_event_alignment(
                config,
                question["question"],
                rubrics,
                response_lines,
            )
            if response_lines
            else []
        )
        return question["id"], event_ordering_metrics(
            rubrics,
            response_lines,
            mapping,
        )

    event_metrics: dict[str, dict[str, Any]] = {}
    if event_questions:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(config.concurrency, len(event_questions)),
        ) as executor:
            event_metrics = dict(executor.map(align_event, event_questions))

    category_scores: dict[str, list[float]] = {}
    for question in questions.values():
        scores = [criterion["score"] for criterion in question["criteria"]]
        question["llm_judge_score"] = sum(scores) / len(scores)
        if question["category"] == "event_ordering":
            ordering = event_metrics[question["id"]]
            question["event_ordering"] = ordering
            # Official BEAM report_results.py uses tau_norm for this ability.
            question["score"] = ordering["tau_norm"]
        else:
            question["score"] = question["llm_judge_score"]
        category_scores.setdefault(question["category"], []).append(
            question["score"],
        )
    categories = {
        category: {
            "score": sum(scores) / len(scores),
            "questions": len(scores),
        }
        for category, scores in sorted(category_scores.items())
    }
    # Upstream reports one mean per ability. Harbor needs one scalar reward,
    # so use an equal-weight macro mean across those official category scores.
    category_values = [item["score"] for item in categories.values()]
    overall = (
        sum(category_values) / len(category_values) if category_values else 0.0
    )
    return {
        "judge": {
            "variant": JUDGE_VARIANT,
            "model": config.model,
            "base_url": config.base_url,
            "temperature": 0,
            "official_source": OFFICIAL_SOURCE,
            "official_commit": OFFICIAL_BEAM_COMMIT,
            "harbor_reward_aggregation": "macro_mean_of_ability_scores",
            "compatibility_notes": [
                "Uses the official unified rubric prompt and category aggregation.",
                "Uses normalized Kendall tau for event_ordering.",
                "Batches semantic event alignment into one judge call per question.",
                "Populates the question placeholder left unresolved upstream.",
                "Preserves official 0.5 judgments instead of truncating them to zero.",
            ],
        },
        "overall": overall,
        "categories": categories,
        "questions": list(questions.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--answers", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reward-file", required=True)
    args = parser.parse_args()

    probing = _load_json_object(args.questions, "probing questions")
    answers = _load_json_object(args.answers, "answers")
    report = score_answers(probing, answers, JudgeConfig.from_env())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    reward_path = Path(args.reward_file)
    reward_path.parent.mkdir(parents=True, exist_ok=True)
    reward_path.write_text(f"{report['overall']:.8f}\n", encoding="utf-8")
    print(
        json.dumps(
            {"overall": report["overall"], "categories": report["categories"]},
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
