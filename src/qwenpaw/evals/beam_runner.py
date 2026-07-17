# -*- coding: utf-8 -*-
"""Run the BEAM long-term-memory benchmark with QwenPaw.

BEAM stores one large conversation in ``chat.json`` and a set of probes in
``questions.json``.  The conversation is replayed verbatim into Scroll's
durable history once; every probe is then asked in a fresh session so probes
cannot inherit one another's active context.

Only the public ``chat.json`` and ``questions.json`` files are read.  Rubrics
and reference answers under ``tests/`` and ``solution/`` are deliberately not
part of this runner's interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from qwenpaw.agents.context.scroll.serialize import strip_headline

logger = logging.getLogger("qwenpaw.evals.beam")

BEAM_HISTORY_KIND = "beam_chat_turn"
_CHUNK_SIZE = 1024 * 1024
_DATE_FORMATS = ("%B-%d-%Y", "%b-%d-%Y")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class IngestStats:
    sessions: int
    messages: int
    rows: int
    elapsed_seconds: float


def _safe_id(value: str) -> str:
    normalized = _SAFE_ID_RE.sub("_", value).strip("_")
    return normalized or "beam"


def iter_json_array(path: str | Path) -> Iterator[Any]:
    """Yield a top-level JSON array incrementally.

    BEAM chat files are tens of megabytes.  This small streaming decoder keeps
    only one top-level batch plus a bounded input buffer resident at a time and
    avoids adding an ``ijson`` dependency to the core package.
    """
    # pylint: disable=too-many-branches,too-many-statements

    decoder = json.JSONDecoder()
    with Path(path).open("r", encoding="utf-8") as source:
        buffer = ""
        pos = 0
        eof = False
        started = False
        expect_value = True

        def refill() -> bool:
            nonlocal buffer, pos, eof
            if eof:
                return False
            if pos:
                buffer = buffer[pos:]
                pos = 0
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                eof = True
                return False
            buffer += chunk
            return True

        while True:
            while pos >= len(buffer) and refill():
                pass
            while pos < len(buffer) and buffer[pos].isspace():
                pos += 1
                if pos >= len(buffer):
                    refill()

            if not started:
                if pos >= len(buffer) and not refill():
                    raise ValueError(f"empty JSON input: {path}")
                if buffer[pos] != "[":
                    raise ValueError(f"expected top-level JSON array: {path}")
                pos += 1
                started = True
                continue

            while pos >= len(buffer) and refill():
                pass
            while pos < len(buffer) and buffer[pos].isspace():
                pos += 1
                if pos >= len(buffer):
                    refill()

            if pos < len(buffer) and buffer[pos] == "]":
                return

            if not expect_value:
                if pos >= len(buffer) and not refill():
                    raise ValueError(f"unterminated JSON array: {path}")
                if buffer[pos] != ",":
                    raise ValueError(
                        "expected ',' between JSON array items at "
                        f"{pos}: {path}",
                    )
                pos += 1
                expect_value = True
                continue

            while True:
                try:
                    value, end = decoder.raw_decode(buffer, pos)
                    pos = end
                    expect_value = False
                    yield value
                    break
                except json.JSONDecodeError as exc:
                    if not refill():
                        raise ValueError(
                            f"invalid or truncated JSON array {path}: {exc}",
                        ) from exc


def _parse_anchor(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    logger.warning("Unrecognized BEAM time_anchor: %r", value)
    return None


def _batch_anchor(batch: dict[str, Any]) -> str | None:
    for group in batch.get("turns", []):
        for message in group:
            anchor = message.get("time_anchor")
            if anchor:
                return str(anchor)
    return None


def _prepare_history(db_path: Path, replace_history: bool) -> None:
    existing = [
        Path(str(db_path) + suffix)
        for suffix in ("", "-wal", "-shm")
        if Path(str(db_path) + suffix).exists()
    ]
    if existing and not replace_history:
        raise RuntimeError(
            f"Refusing to replace existing Scroll history at {db_path}. "
            "Use a dedicated benchmark agent/workspace, or pass "
            "--replace-history explicitly.",
        )
    for path in existing:
        path.unlink()


def ingest_chat(
    workspace: Any,
    chat_path: str | Path,
    *,
    conversation_id: str,
    replace_history: bool,
) -> IngestStats:
    """Replay one BEAM conversation into the workspace's Scroll history."""

    from agentscope.message import Msg, TextBlock

    from qwenpaw.agents.context.scroll.history import HistoryStore
    from qwenpaw.agents.context.scroll.serialize import msg_to_entries

    db_path = Path(workspace.workspace_dir) / "history.db"
    _prepare_history(db_path, replace_history)
    history = HistoryStore(db_path)
    session_count = message_count = row_count = 0
    started = time.monotonic()
    try:
        for batch_position, batch in enumerate(iter_json_array(chat_path), 1):
            if not isinstance(batch, dict):
                raise ValueError(
                    f"BEAM batch {batch_position} must be an object",
                )
            batch_number = int(batch.get("batch_number", batch_position))
            anchor = _batch_anchor(batch)
            created_at = _parse_anchor(anchor)
            session_id = (
                f"beam__{_safe_id(conversation_id)}__batch_{batch_number:03d}"
            )
            session_count += 1

            for group_position, group in enumerate(batch.get("turns", []), 1):
                if not isinstance(group, list):
                    raise ValueError(
                        f"BEAM batch {batch_number} turn group "
                        f"{group_position} must be an array",
                    )
                for message_position, raw in enumerate(group, 1):
                    if not isinstance(raw, dict):
                        raise ValueError(
                            f"BEAM batch {batch_number} message must be "
                            "an object",
                        )
                    role = str(raw.get("role") or "user")
                    content = str(raw.get("content") or "")
                    msg = Msg(
                        name=role,
                        role=role,
                        content=[TextBlock(type="text", text=content)],
                    )
                    metadata = {
                        "benchmark": "beam",
                        "conversation_id": conversation_id,
                        "batch_number": batch_number,
                        "group_position": group_position,
                        "message_position": message_position,
                        "message_id": raw.get("id"),
                        "index": raw.get("index"),
                        "question_type": raw.get("question_type"),
                        "time_anchor": raw.get("time_anchor") or anchor,
                    }
                    metadata = {
                        key: value
                        for key, value in metadata.items()
                        if value is not None
                    }
                    for entry_position, entry in enumerate(
                        msg_to_entries(msg),
                        1,
                    ):
                        prepared = replace(
                            entry,
                            kind=BEAM_HISTORY_KIND,
                            metadata=metadata,
                            created_at=created_at,
                        )
                        raw_id = raw.get("id", message_position)
                        dedup_key = (
                            f"beam:{conversation_id}:{batch_number}:"
                            f"{group_position}:{raw_id}:{entry_position}"
                        )
                        history.append(
                            session_id=session_id,
                            agent_id=getattr(workspace, "agent_id", None),
                            entry=prepared,
                            dedup_key=dedup_key,
                        )
                        row_count += 1
                    message_count += 1
    finally:
        history.close()

    return IngestStats(
        sessions=session_count,
        messages=message_count,
        rows=row_count,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def load_questions(path: str | Path, limit: int = 0) -> list[dict[str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("BEAM questions.json must contain an array")
    questions: list[dict[str, str]] = []
    seen: set[str] = set()
    for position, raw in enumerate(data, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"BEAM question {position} must be an object")
        qid = str(raw.get("id") or "").strip()
        qtype = str(raw.get("type") or "").strip()
        question = str(raw.get("question") or "").strip()
        if not qid or not qtype or not question:
            raise ValueError(
                f"BEAM question {position} requires id, type, and question",
            )
        if qid in seen:
            raise ValueError(f"duplicate BEAM question id: {qid}")
        seen.add(qid)
        questions.append({"id": qid, "type": qtype, "question": question})
        if limit and len(questions) >= limit:
            break
    return questions


_STRUCTURED_PROMPT = (
    "The user's benchmark conversation history is stored in your durable "
    f"history as rows with kind='{BEAM_HISTORY_KIND}'. Use the "
    "recall_history tool to search those rows before answering. Search with "
    "concise keyword or synonym queries when needed, using all_agents=true "
    f"and kind='{BEAM_HISTORY_KIND}'. A search result already includes the "
    "complete user-bounded turn around each match, together with its "
    "created_at timestamp; do not call expand merely to pair a user message "
    "with its assistant reply. For a question about one source date, pass "
    "created_on='YYYY-MM-DD'; for an inclusive source-date range, pass "
    "created_from and created_to. Date filters may be used with an empty "
    "query. For elapsed calendar days, use op='days_between' with the two "
    "recalled dates. If you must reread a returned turn, expand only its "
    "exact turn_start_seq through turn_end_seq and pass lo and hi as "
    "unquoted JSON integers. Treat role='user' rows as evidence of the "
    "user's facts, actions, and preferences; an assistant suggestion is not "
    "evidence that the user adopted it. When a fact changed, compare the "
    "relevant created_at values and use the latest applicable user evidence. "
    "Base your answer only on recalled conversation evidence. Do not use "
    "information from other history kinds. Follow any output-count or "
    "formatting constraint in the question exactly. If the requested fact "
    "is absent, say so clearly."
    "\n\nQuestion: "
)

_PYTHON_PROMPT = (
    "The user's benchmark conversation history is stored in your durable "
    f"history as rows with kind='{BEAM_HISTORY_KIND}'. Use the "
    "recall_history_python tool to search those rows before answering. Search "
    "with concise keyword or synonym queries when needed, using "
    f"ms.search(QUERY, all_agents=True, kind='{BEAM_HISTORY_KIND}', k=20). "
    "By default each result already includes the complete user-bounded "
    "turn around the match and its created_at timestamp; do not call "
    "ms.expand merely to pair a user message with its assistant reply. For "
    "one source date, pass created_on='YYYY-MM-DD'; for an inclusive source-"
    "date range, pass created_from and created_to. Date filters may be used "
    "with an empty query. Use ms.days_between(start, end) for elapsed "
    "calendar days. If you must reread a returned turn, call ms.expand "
    "only with that result's exact turn_start_seq and turn_end_seq. "
    "Treat role='user' rows as evidence of the user's facts, actions, and "
    "preferences; an assistant suggestion is not evidence that the user "
    "adopted it. When a fact changed, compare the relevant created_at values "
    "and use the latest applicable user evidence. Base your answer only on "
    "recalled conversation evidence. Do not use information from other "
    "history kinds. Follow any output-count or formatting constraint in the "
    "question exactly. If the requested fact is absent, say so clearly."
    "\n\nQuestion: "
)


def _extract_answer(events: list[Any]) -> str:
    """Return only the final completed assistant message for the probe.

    ``stream_query`` finalizes every visible pre-tool progress message as a
    normal assistant message. Concatenating all non-delta content events
    therefore leaks text such as "Let me search..." into the response sent to
    the BEAM judge. The completed message envelope already owns its full text
    blocks, so select the last one and apply the same headline cleanup used by
    user-facing channels.
    """
    final_message: Any | None = None
    for event in events:
        if getattr(event, "object", None) != "message":
            continue
        event_type = getattr(event, "type", None)
        event_type = getattr(event_type, "value", event_type)
        if event_type != "message":
            continue
        role = getattr(event, "role", None)
        role = getattr(role, "value", role)
        if role != "assistant":
            continue
        status = getattr(event, "status", None)
        status = getattr(status, "value", status)
        if status == "completed":
            final_message = event

    if final_message is None:
        return ""

    parts: list[str] = []
    for block in getattr(final_message, "content", None) or []:
        text = (
            block.get("text", "")
            if isinstance(block, dict)
            else getattr(block, "text", "")
        )
        if text:
            parts.append(str(text))
    answer = "".join(parts).strip()
    return (strip_headline(answer) or "").strip()


def _extract_tool_steps(events: list[Any]) -> list[dict[str, str]]:
    calls: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for event in events:
        if getattr(event, "object", None) != "message":
            continue
        event_type = getattr(event, "type", None)
        event_type = getattr(event_type, "value", event_type)
        if event_type not in ("plugin_call", "plugin_call_output"):
            continue
        content = getattr(event, "content", None) or []
        if not content:
            continue
        data = getattr(content[0], "data", None)
        if not isinstance(data, dict):
            continue
        call_id = str(data.get("call_id") or f"call-{len(order)}")
        step = calls.setdefault(
            call_id,
            {"name": "", "arguments": "", "output": ""},
        )
        if call_id not in order:
            order.append(call_id)
        step["name"] = str(data.get("name") or step["name"])
        if event_type == "plugin_call":
            arguments = data.get("arguments")
            step["arguments"] = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False, default=str)
            )
        else:
            output = data.get("output")
            step["output"] = (
                output
                if isinstance(output, str)
                else json.dumps(output, ensure_ascii=False, default=str)
            )
    return [calls[call_id] for call_id in order]


def _extract_usage(events: list[Any]) -> dict[str, int]:
    by_message: dict[str, tuple[int, int]] = {}
    for event in events:
        usage = getattr(event, "usage", None)
        if not isinstance(usage, dict):
            continue
        message_id = str(getattr(event, "id", None) or id(event))
        by_message[message_id] = (
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )
    return {
        "input_tokens": sum(item[0] for item in by_message.values()),
        "output_tokens": sum(item[1] for item in by_message.values()),
    }


def _jsonable_event(event: Any) -> Any:
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    if isinstance(event, dict):
        return event
    return {"repr": repr(event)}


async def ask_probe(
    workspace: Any,
    *,
    agent_id: str,
    conversation_id: str,
    probe: dict[str, str],
    recall_tool: str,
    trace_dir: Path | None,
) -> tuple[str, dict[str, Any]]:
    prefix = (
        _STRUCTURED_PROMPT if recall_tool == "structured" else _PYTHON_PROMPT
    )
    prompt = prefix + probe["question"]
    session_id = (
        f"beam__{_safe_id(conversation_id)}__probe__{_safe_id(probe['id'])}"
    )
    request = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        ],
        "session_id": session_id,
        "user_id": session_id,
        "agent_id": agent_id,
    }
    started = time.monotonic()
    events: list[Any] = []
    async for event in workspace.stream_query(request):
        events.append(event)
    elapsed = round(time.monotonic() - started, 3)
    answer = _extract_answer(events)
    tool_steps = _extract_tool_steps(events)
    usage = _extract_usage(events)

    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = trace_dir / f"{_safe_id(probe['id'])}.json"
        trace_path.write_text(
            json.dumps(
                {
                    "probe": probe,
                    "prompt": prompt,
                    "answer": answer,
                    "elapsed_seconds": elapsed,
                    "usage": usage,
                    "tool_steps": tool_steps,
                    "events": [_jsonable_event(event) for event in events],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    return answer, {
        "id": probe["id"],
        "type": probe["type"],
        "elapsed_seconds": elapsed,
        "recall_calls": len(tool_steps),
        **usage,
    }


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


async def run_benchmark(
    *,
    chat_path: Path,
    questions_path: Path,
    out_path: Path,
    metrics_path: Path,
    trace_dir: Path | None,
    agent_id: str,
    model: str | None,
    conversation_id: str,
    recall_tool: str,
    limit_probes: int,
    replace_history: bool,
) -> dict[str, Any]:
    if model:
        await _configure_model(agent_id, model)
    _configure_benchmark_history(agent_id)

    from qwenpaw.app.multi_agent_manager import MultiAgentManager

    if recall_tool == "structured":
        # This must be set before the workspace builds its tool registry.
        os.environ["QWENPAW_DISABLE_RECALL_PYTHON"] = "1"

    manager = MultiAgentManager()
    workspace = await manager.get_agent(agent_id)
    answers: dict[str, list[dict[str, str]]] = {}
    probe_metrics: list[dict[str, Any]] = []
    run_started = time.monotonic()
    try:
        ingestion = ingest_chat(
            workspace,
            chat_path,
            conversation_id=conversation_id,
            replace_history=replace_history,
        )
        questions = load_questions(questions_path, limit_probes)
        _write_json_atomic(out_path, answers)
        _write_json_atomic(
            metrics_path,
            {
                "benchmark": "beam",
                "conversation_id": conversation_id,
                "agent_id": agent_id,
                "model": model,
                "recall_tool": recall_tool,
                "ingestion": ingestion.__dict__,
                "probes": probe_metrics,
                "elapsed_seconds": round(time.monotonic() - run_started, 3),
            },
        )

        for position, probe in enumerate(questions, 1):
            logger.info(
                "BEAM probe %s/%s: %s",
                position,
                len(questions),
                probe["id"],
            )
            metric: dict[str, Any]
            try:
                response, metric = await ask_probe(
                    workspace,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    probe=probe,
                    recall_tool=recall_tool,
                    trace_dir=trace_dir,
                )
                metric["status"] = "success"
            except Exception as exc:  # continue and preserve partial results
                logger.exception("BEAM probe %s failed", probe["id"])
                response = ""
                metric = {
                    "id": probe["id"],
                    "type": probe["type"],
                    "status": "error",
                    "error": str(exc),
                    "elapsed_seconds": 0,
                    "recall_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            answers.setdefault(probe["type"], []).append(
                {
                    "id": probe["id"],
                    "question": probe["question"],
                    "llm_response": response,
                },
            )
            probe_metrics.append(metric)
            _write_json_atomic(out_path, answers)
            _write_json_atomic(
                metrics_path,
                {
                    "benchmark": "beam",
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                    "model": model,
                    "recall_tool": recall_tool,
                    "ingestion": ingestion.__dict__,
                    "probes": probe_metrics,
                    "elapsed_seconds": round(
                        time.monotonic() - run_started,
                        3,
                    ),
                },
            )
    finally:
        await manager.stop_all()

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["totals"] = {
        "probes": len(probe_metrics),
        "succeeded": sum(
            item.get("status") == "success" for item in probe_metrics
        ),
        "failed": sum(
            item.get("status") != "success" for item in probe_metrics
        ),
        "input_tokens": sum(
            item.get("input_tokens", 0) for item in probe_metrics
        ),
        "output_tokens": sum(
            item.get("output_tokens", 0) for item in probe_metrics
        ),
        "recall_calls": sum(
            item.get("recall_calls", 0) for item in probe_metrics
        ),
    }
    _write_json_atomic(metrics_path, metrics)
    return metrics


async def _configure_model(agent_id: str, model: str) -> None:
    """Configure Harbor's ``provider/model`` selection for QwenPaw.

    Harbor forwards provider credentials as environment variables.  QwenPaw
    normally stores provider credentials through its interactive setup, so a
    headless benchmark run bridges those variables into the provider manager
    before the workspace is built.
    """

    from qwenpaw.config.config import (
        ModelSlotConfig,
        load_agent_config,
        save_agent_config,
    )
    from qwenpaw.providers.provider import ModelInfo
    from qwenpaw.providers.provider_manager import ProviderManager

    if "/" not in model:
        raise ValueError("--model must use Harbor's provider/model format")
    provider_id, model_id = model.split("/", 1)
    if not provider_id or not model_id:
        raise ValueError("--model must use Harbor's provider/model format")

    manager = ProviderManager.get_instance()
    provider = manager.get_provider(provider_id)
    if provider is None:
        raise ValueError(f"QwenPaw provider not found: {provider_id}")

    prefix = re.sub(r"[^A-Za-z0-9]", "_", provider_id).upper()
    api_key = (
        os.getenv("QWENPAW_MODEL_API_KEY", "").strip()
        or os.getenv(f"{prefix}_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("QWENPAW_MODEL_BASE_URL", "").strip()
        or os.getenv(f"{prefix}_BASE_URL", "").strip()
    )
    updates: dict[str, str] = {}
    if api_key:
        updates["api_key"] = api_key
    if updates and not manager.update_provider(provider_id, updates):
        raise RuntimeError(f"failed to configure provider: {provider_id}")
    # A benchmark endpoint is an explicit per-run override, including for
    # built-ins whose URL is intentionally frozen in the interactive UI.
    if base_url:
        provider.base_url = base_url
    if not provider.has_model(model_id):
        await provider.add_model(ModelInfo(id=model_id, name=model_id))

    await manager.activate_model(provider_id, model_id)
    agent_config = load_agent_config(agent_id)
    agent_config.active_model = ModelSlotConfig(
        provider_id=provider_id,
        model=model_id,
    )
    save_agent_config(agent_id, agent_config)


def _configure_benchmark_history(agent_id: str) -> None:
    """Keep imported BEAM history for the lifetime of the benchmark run.

    BEAM preserves the original timestamps of its multi-session conversation.
    Those timestamps can be much older than Scroll's normal 30-day retention
    window, so an agent teardown after the first probe would otherwise purge
    the imported benchmark context. Harbor runs this agent in an isolated,
    disposable workspace, making unbounded retention appropriate here.
    """

    from qwenpaw.config.config import load_agent_config, save_agent_config

    agent_config = load_agent_config(agent_id)
    scroll_config = agent_config.running.light_context_config.scroll_config
    scroll_config.history_retention_days = 0
    save_agent_config(agent_id, agent_config)
    logger.info(
        "BEAM benchmark history retention disabled for agent %s",
        agent_id,
    )


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    task_dir = Path(args.task_dir).resolve() if args.task_dir else None
    if args.chat:
        chat = Path(args.chat)
    elif task_dir is not None:
        chat = task_dir / "chat.json"
        if not chat.exists():
            chat = task_dir / "environment" / "chat.json"
    else:
        raise ValueError("--chat is required when --task-dir is omitted")
    if args.questions:
        questions = Path(args.questions)
    elif task_dir is not None:
        questions = task_dir / "questions.json"
        if not questions.exists():
            questions = task_dir / "environment" / "questions.json"
    else:
        raise ValueError("--questions is required when --task-dir is omitted")
    output_dir = task_dir if task_dir is not None else Path.cwd()
    out = Path(args.out) if args.out else output_dir / "answers.json"
    return chat.resolve(), questions.resolve(), out.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-dir",
        help="directory containing chat/questions",
    )
    parser.add_argument("--chat", help="BEAM chat.json path")
    parser.add_argument("--questions", help="public BEAM questions.json path")
    parser.add_argument("--out", help="answers.json output path")
    parser.add_argument("--metrics-out", help="metrics JSON output path")
    parser.add_argument("--trace-dir", help="raw per-probe trace directory")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--model",
        help=(
            "Harbor-style provider/model override "
            "(for example dashscope/qwen3-max)"
        ),
    )
    parser.add_argument("--conversation-id", default="1")
    parser.add_argument(
        "--recall-tool",
        choices=("structured", "python"),
        default="structured",
    )
    parser.add_argument("--limit-probes", type=int, default=0)
    parser.add_argument(
        "--replace-history",
        action="store_true",
        help="delete the selected agent workspace's existing history.db",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.task_dir and (not args.chat or not args.questions):
        parser.error("provide --task-dir, or both --chat and --questions")
    chat, questions, out = _resolve_paths(args)
    if not chat.is_file():
        parser.error(f"chat file does not exist: {chat}")
    if not questions.is_file():
        parser.error(f"questions file does not exist: {questions}")
    metrics = (
        Path(args.metrics_out).resolve()
        if args.metrics_out
        else out.with_name("metrics.json")
    )
    trace_dir = Path(args.trace_dir).resolve() if args.trace_dir else None
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), 20))
    result = asyncio.run(
        run_benchmark(
            chat_path=chat,
            questions_path=questions,
            out_path=out,
            metrics_path=metrics,
            trace_dir=trace_dir,
            agent_id=args.agent_id,
            model=args.model,
            conversation_id=args.conversation_id,
            recall_tool=args.recall_tool,
            limit_probes=max(0, args.limit_probes),
            replace_history=args.replace_history,
        ),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
