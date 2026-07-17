# -*- coding: utf-8 -*-
"""Convert QwenPaw BEAM probe traces to Harbor's ATIF trajectory shape."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {"value": parsed}
    return {"value": value}


def _reasoning_content(events: Any) -> str | None:
    if not isinstance(events, list):
        return None
    seen: set[str] = set()
    parts: list[str] = []
    for position, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "").lower()
        if event.get("object") != "message" or "reason" not in event_type:
            continue
        event_id = str(event.get("id") or f"reasoning-{position}")
        if event_id in seen:
            continue
        seen.add(event_id)
        content = event.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping)
        ).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts) or None


def _llm_call_count(events: Any) -> int | None:
    if not isinstance(events, list):
        return None
    calls: set[str] = set()
    for position, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        usage = event.get("usage")
        if event.get("object") != "message" or not isinstance(usage, Mapping):
            continue
        calls.add(str(event.get("id") or f"usage-{position}"))
    return len(calls) or None


def _probe_steps(
    trace: Mapping[str, Any],
    step_id: int,
) -> list[dict[str, Any]]:
    probe_raw = trace.get("probe")
    probe = probe_raw if isinstance(probe_raw, Mapping) else {}
    probe_id = str(probe.get("id") or f"probe-{step_id}")
    probe_type = str(probe.get("type") or "unknown")
    question = str(probe.get("question") or "")
    prompt = str(trace.get("prompt") or question)
    answer = str(trace.get("answer") or "")
    elapsed_seconds = float(trace.get("elapsed_seconds") or 0)

    usage_raw = trace.get("usage")
    usage = usage_raw if isinstance(usage_raw, Mapping) else {}
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))

    tool_calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    tool_steps_raw = trace.get("tool_steps")
    tool_steps = tool_steps_raw if isinstance(tool_steps_raw, list) else []
    for tool_position, tool_step in enumerate(tool_steps, 1):
        if not isinstance(tool_step, Mapping):
            continue
        call_id = f"{probe_id}-tool-{tool_position}"
        tool_calls.append(
            {
                "tool_call_id": call_id,
                "function_name": str(tool_step.get("name") or "unknown"),
                "arguments": _tool_arguments(tool_step.get("arguments")),
            },
        )
        results.append(
            {
                "source_call_id": call_id,
                "content": str(tool_step.get("output") or ""),
            },
        )

    probe_extra = {
        "benchmark": "beam",
        "probe_id": probe_id,
        "probe_type": probe_type,
        "probe_question": question,
        "probe_sessions_independent": True,
        "raw_trace_path": f"traces/{probe_id}.json",
    }
    agent_step: dict[str, Any] = {
        "step_id": step_id + 1,
        "source": "agent",
        "message": answer,
        "metrics": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "extra": {"elapsed_seconds": elapsed_seconds},
        },
        "extra": {
            **probe_extra,
            "recall_calls": len(tool_calls),
        },
    }
    reasoning = _reasoning_content(trace.get("events"))
    if reasoning:
        agent_step["reasoning_content"] = reasoning
    call_count = _llm_call_count(trace.get("events"))
    if call_count is not None:
        agent_step["llm_call_count"] = call_count
    if tool_calls:
        agent_step["tool_calls"] = tool_calls
        agent_step["observation"] = {"results": results}

    return [
        {
            "step_id": step_id,
            "source": "user",
            "message": prompt,
            "extra": probe_extra,
        },
        agent_step,
    ]


def build_beam_trajectory(
    traces: Iterable[Mapping[str, Any]],
    *,
    session_id: str,
    agent_version: str,
    model_name: str | None,
    totals: Mapping[str, Any] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Build one viewer-friendly ATIF-v1.7 trajectory from probe traces."""

    steps: list[dict[str, Any]] = []
    for trace in traces:
        steps.extend(_probe_steps(trace, len(steps) + 1))
    if not steps:
        raise ValueError("at least one BEAM probe trace is required")

    summary = totals or {}
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "trajectory_id": session_id,
        "agent": {
            "name": "qwenpaw-beam",
            "version": agent_version,
            "model_name": model_name,
        },
        "steps": steps,
        "notes": (
            "BEAM probes are executed in isolated QwenPaw sessions. Step "
            "order "
            "records benchmark execution order and does not imply that one "
            "probe's active context was provided to the next probe."
        ),
        "final_metrics": {
            "total_prompt_tokens": _as_int(summary.get("input_tokens")),
            "total_completion_tokens": _as_int(summary.get("output_tokens")),
            "total_steps": len(steps),
            "extra": {
                "probes": len(steps) // 2,
                "recall_calls": _as_int(summary.get("recall_calls")),
            },
        },
        "extra": {
            "benchmark": "beam",
            "conversation_id": conversation_id,
            "probe_sessions_independent": True,
            "raw_trace_directory": "traces",
        },
    }


def load_ordered_traces(
    agent_dir: str | Path,
    metrics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Load trace JSON in the probe execution order recorded by metrics."""

    trace_dir = Path(agent_dir) / "traces"
    trace_paths = {path.stem: path for path in trace_dir.glob("*.json")}
    traces: list[dict[str, Any]] = []
    probes = metrics.get("probes")
    for probe in probes if isinstance(probes, list) else []:
        if not isinstance(probe, Mapping):
            continue
        probe_id = str(probe.get("id") or "")
        path = trace_paths.pop(probe_id, None)
        if path is not None:
            traces.append(json.loads(path.read_text(encoding="utf-8")))
    for path in sorted(trace_paths.values()):
        traces.append(json.loads(path.read_text(encoding="utf-8")))
    return traces


def write_beam_trajectory(
    agent_dir: str | Path,
    *,
    session_id: str | None = None,
    agent_version: str = "unknown",
    model_name: str | None = None,
) -> Path:
    """Backfill ``agent/trajectory.json`` for an existing Harbor trial."""

    directory = Path(agent_dir)
    metrics = json.loads(
        (directory / "metrics.json").read_text(encoding="utf-8"),
    )
    if not isinstance(metrics, Mapping):
        raise ValueError("BEAM metrics.json must contain an object")
    traces = load_ordered_traces(directory, metrics)
    trajectory = build_beam_trajectory(
        traces,
        session_id=session_id or directory.parent.name,
        agent_version=agent_version,
        model_name=model_name or str(metrics.get("model") or "") or None,
        totals=(
            metrics.get("totals")
            if isinstance(metrics.get("totals"), Mapping)
            else None
        ),
        conversation_id=str(metrics.get("conversation_id") or ""),
    )
    output = directory / "trajectory.json"
    output.write_text(
        json.dumps(trajectory, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert QwenPaw BEAM probe traces to Harbor ATIF-v1.7",
    )
    parser.add_argument(
        "agent_dir",
        help=(
            "Harbor trial's agent directory containing metrics.json and "
            "traces/"
        ),
    )
    parser.add_argument("--session-id")
    parser.add_argument("--agent-version", default="unknown")
    parser.add_argument("--model-name")
    args = parser.parse_args()
    output = write_beam_trajectory(
        args.agent_dir,
        session_id=args.session_id,
        agent_version=args.agent_version,
        model_name=args.model_name,
    )
    print(output)


if __name__ == "__main__":
    main()
