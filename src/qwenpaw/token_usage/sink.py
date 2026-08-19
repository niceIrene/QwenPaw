# -*- coding: utf-8 -*-
"""Append-only JSONL sink for per-call token usage.

The buffered ``TokenUsageBuffer`` aggregates by day and only flushes every
few seconds from a running event loop, so its data is lost when a process
is killed (e.g. a benchmark trial hitting the wall-clock limit). This sink
writes one line per LLM call, synchronously, so every completed call is
already on disk before the next call starts.

Enabled by setting ``QWENPAW_USAGE_JSONL`` to a file path.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_ENV_VAR = "QWENPAW_USAGE_JSONL"
_META_ENV_VAR = "QWENPAW_REQUEST_META_JSON"
_OTEL_ENV_VAR = "QWENPAW_OTEL_JSONL"


def _env_path(var: str) -> Path | None:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def configured_usage_jsonl_path() -> Path | None:
    """Return the sink path from the environment, or None when disabled."""
    return _env_path(_ENV_VAR)


def configured_request_meta_path() -> Path | None:
    """Return the request-meta path from the environment, or None."""
    return _env_path(_META_ENV_VAR)


def configured_otel_jsonl_path() -> Path | None:
    """Return the OTel span JSONL path from the environment, or None."""
    return _env_path(_OTEL_ENV_VAR)


def append_usage_record(path: Path, record: dict) -> None:
    """Append *record* as one JSON line. Best-effort; never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        # O_APPEND keeps concurrent writers in the same process safe and
        # avoids truncation races between multiple wrappers.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("token_usage: failed to append %s: %s", path, exc)


def write_request_meta(path: Path, record: dict) -> None:
    """Write *record* as a JSON document. Best-effort; never raises."""
    from ..utils.io_utils import write_json_atomic

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, record, indent=None, new_file_mode=0o644)
    except OSError as exc:
        logger.warning("token_usage: failed to write %s: %s", path, exc)


__all__ = [
    "append_usage_record",
    "configured_otel_jsonl_path",
    "configured_request_meta_path",
    "configured_usage_jsonl_path",
    "write_request_meta",
]
