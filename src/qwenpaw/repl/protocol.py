# -*- coding: utf-8 -*-
"""JSON-Lines protocol for the CodeAct REPL (design doc §2.2)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TextIO

MAX_KERNEL_MESSAGE_BYTES = 256 * 1024
MAX_TOOL_RESULT_BYTES = 16 * 1024 * 1024

MESSAGE_TYPES = frozenset(
    {
        "init",
        "exec",
        "interrupt",
        "tool_call",
        "tool_result",
        "exec_result",
        "restore",
        "restore_result",
        "tool_list_update",
        "shutdown",
        "log",
        "lm_call",
        "lm_result",
    },
)


class ProtocolError(ValueError):
    """Raised when a REPL protocol message is malformed or oversized."""


def validate_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the common protocol envelope and return a plain dictionary."""
    if not isinstance(message, Mapping):
        raise ProtocolError("protocol message must be a JSON object")
    message_id = message.get("id")
    message_type = message.get("type")
    if not isinstance(message_id, str) or not message_id:
        raise ProtocolError("protocol message requires a non-empty string id")
    if not isinstance(message_type, str) or message_type not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown protocol message type: {message_type!r}")
    return dict(message)


def encode_message(
    message: Mapping[str, Any],
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Encode one validated message as a UTF-8 JSON line."""
    payload = (
        json.dumps(
            validate_message(message),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if max_bytes is not None and len(payload) > max_bytes:
        raise ProtocolError(
            f"protocol message is {len(payload)} bytes; limit is {max_bytes}",
        )
    return payload


def decode_message(
    payload: str | bytes,
    *,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Decode one JSON line and validate its common envelope."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if max_bytes is not None and len(raw) > max_bytes:
        raise ProtocolError(
            f"protocol message is {len(raw)} bytes; limit is {max_bytes}",
        )
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid JSON protocol message: {exc}") from exc
    return validate_message(decoded)


def read_message(
    stream: TextIO,
    *,
    max_bytes: int | None = None,
) -> dict[str, Any] | None:
    """Read one message from a synchronous stream; return ``None`` at EOF."""
    line = stream.readline()
    if line == "":
        return None
    return decode_message(line, max_bytes=max_bytes)


def write_message(
    stream: TextIO,
    message: Mapping[str, Any],
    *,
    max_bytes: int | None = None,
) -> None:
    """Write and flush one message to a synchronous text stream."""
    payload = encode_message(message, max_bytes=max_bytes)
    binary_stream = getattr(stream, "buffer", None)
    if binary_stream is not None:
        binary_stream.write(payload)
    else:
        stream.write(payload.decode("utf-8"))
    stream.flush()


__all__ = [
    "MAX_KERNEL_MESSAGE_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "MESSAGE_TYPES",
    "ProtocolError",
    "decode_message",
    "encode_message",
    "read_message",
    "validate_message",
    "write_message",
]
