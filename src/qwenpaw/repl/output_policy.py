# -*- coding: utf-8 -*-
"""Bounded REPL output and spill files (design doc §2.4 and §2.9-D2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_STDOUT_LIMIT = 8192
DEFAULT_TRACEBACK_LIMIT = 16 * 1024

# The per-cell stdout cap scales with the model's context window, like the
# Scroll reference runtime (``stdout_cap_for``): a quarter of the window,
# clamped. A fixed 8 KB suits a 32k-token model; on a 1M-token model it cut a
# fifth of all recall cells in a long-memory benchmark, and each cut cost a
# re-query turn. The unit here is bytes, as everywhere in this module.
MIN_STDOUT_LIMIT = 2 * 1024
MAX_STDOUT_LIMIT = 32 * 1024
# On overflow only a short head is shown: the point of the notice is to make
# the model re-print less from its variables, not to work from a fragment.
OVERFLOW_HEAD_BYTES = 1536
# Public so callers can recognise an already-bounded observation.
OVERFLOW_MARKER = "[output too long:"
STDOUT_LIMIT_ENV = "QWENPAW_REPL_STDOUT_LIMIT"


def stdout_limit_for(context_tokens: int | None) -> int:
    """Per-cell stdout cap, in bytes, for a model context window in tokens.

    Unknown or non-positive windows keep :data:`DEFAULT_STDOUT_LIMIT`.
    """
    if not context_tokens or context_tokens <= 0:
        return DEFAULT_STDOUT_LIMIT
    return max(MIN_STDOUT_LIMIT, min(MAX_STDOUT_LIMIT, context_tokens // 4))


def resolve_stdout_limit(
    explicit: int | None = None,
    context_tokens: int | None = None,
) -> int:
    """Pick the cap: operator env override > explicit value > scaled."""
    import os

    raw = (os.getenv(STDOUT_LIMIT_ENV) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if explicit is not None:
        return explicit
    return stdout_limit_for(context_tokens)

# Output display policy for the final cell expression (roadmap §2.3).
DISPLAY_MODES = ("none", "summary", "full")
DEFAULT_DISPLAY = "summary"
#: summary mode only renders the full repr up to this many bytes; larger
#: values degrade to a bounded type/size/preview line.
SUMMARY_PREVIEW_BYTES = 512


def validate_display(display: Any) -> str:
    """Return a canonical display mode or raise ``ValueError``."""
    if isinstance(display, str) and display in DISPLAY_MODES:
        return display
    raise ValueError(
        f"display must be one of none|summary|full, got {display!r}",
    )


def render_last_expression(
    value: Any,
    display: str = DEFAULT_DISPLAY,
) -> str | None:
    """Render the final cell expression according to the display policy.

    - ``none``:    nothing is shown (returns ``None``);
    - ``summary``: small values render as their repr (current behavior);
      large values degrade to a bounded type/size/preview line;
    - ``full``:    the complete repr is returned; the surrounding output
      budget (spill files) still caps what reaches the model.
    """
    mode = validate_display(display)
    if mode == "none":
        return None
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - hostile user object
        text = f"<unrepresentable {type(value).__name__}>"
    if mode == "full":
        return text
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= SUMMARY_PREVIEW_BYTES:
        return text
    try:
        size = len(value)  # type: ignore[arg-type]
        size_note = f" len={size}"
    except (TypeError, AttributeError):
        size_note = ""
    preview = encoded[:SUMMARY_PREVIEW_BYTES].decode("utf-8", errors="ignore")
    return (
        f"[summary {type(value).__name__}{size_note} "
        f"~{len(encoded)} bytes] {preview}...\n"
        "[preview truncated; re-run with display='full' for the complete "
        "value, or keep transforming the variable instead of printing it]"
    )


@dataclass(frozen=True)
class BoundedOutput:
    """Output text after applying the context-size policy."""

    text: str
    spilled: tuple[str, ...] = ()


def _safe_workspace_path(workspace: Path, relative: str | Path) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(
            "output path must be relative and cannot contain '..'",
        )
    root = workspace.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("output path escapes the REPL workspace")
    return resolved


def _head(encoded: bytes, budget: int) -> str:
    """First ``budget`` bytes, cut back to a line boundary if there is one."""
    if budget <= 0:
        return ""
    chunk = encoded[:budget]
    newline = chunk.rfind(b"\n")
    if newline > 0 and len(encoded) > budget:
        chunk = chunk[: newline + 1]
    return chunk.decode("utf-8", errors="ignore")


def bound_output(
    text: str,
    *,
    workspace: Path,
    spill_name: str,
    limit: int = DEFAULT_STDOUT_LIMIT,
) -> BoundedOutput:
    """Return bounded text, spilling the full UTF-8 payload when necessary.

    Over the limit, the model gets a notice and a short head instead of the
    output: what it needs is normally still in its variables, so the notice
    tells it to print less rather than to repeat the call. The full payload
    is also saved under ``out/`` when the workspace is writable.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return BoundedOutput(text=text)

    lead = (
        f"{OVERFLOW_MARKER} {len(encoded)} bytes printed, over the "
        f"{limit}-byte limit for this model's context window; only the "
        "first part follows. Your variables persist: keep using the "
        "original REPL variables and print LESS (a count, a few fields, a "
        "short slice per row)"
    )
    relative = Path("out") / spill_name
    spilled: tuple[str, ...] = ()
    try:
        destination = _safe_workspace_path(workspace, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
    except OSError:
        # The sandbox can remount the workspace read-only; a failed spill must
        # not fail the cell. Steer to the variables, not to a re-run: "re-run"
        # sends the model back to repeat the retrieval it already did.
        notice = (
            f"{lead} instead of re-running the call. The full output could "
            "not be spilled (workspace read-only).]\n"
        )
    else:
        spilled = (relative.as_posix(),)
        notice = (
            f"{lead}. Full output saved to {relative.as_posix()}: read a "
            f"bounded slice with pathlib.Path({relative.as_posix()!r})"
            ".read_text()[:2000]; do not print the whole spill.]\n"
        )

    notice_bytes = notice.encode("utf-8", errors="replace")
    room = limit - len(notice_bytes)
    bounded = notice + _head(encoded, min(OVERFLOW_HEAD_BYTES, room))
    # A tiny limit must still be a hard cap, even on the notice itself.
    bounded_bytes = bounded.encode("utf-8", errors="replace")
    if len(bounded_bytes) > limit:
        bounded = bounded_bytes[:limit].decode("utf-8", errors="ignore")
    return BoundedOutput(text=bounded, spilled=spilled)


def bound_traceback(
    traceback_text: str,
    *,
    limit: int = DEFAULT_TRACEBACK_LIMIT,
) -> str:
    """Bound a traceback by retaining equal portions of its head and tail."""
    encoded = traceback_text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return traceback_text
    half = max(1, limit // 2)
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    return f"{head}\n...[traceback truncated]...\n{tail}"


__all__ = [
    "BoundedOutput",
    "DEFAULT_DISPLAY",
    "DEFAULT_STDOUT_LIMIT",
    "DEFAULT_TRACEBACK_LIMIT",
    "DISPLAY_MODES",
    "MAX_STDOUT_LIMIT",
    "MIN_STDOUT_LIMIT",
    "OVERFLOW_HEAD_BYTES",
    "OVERFLOW_MARKER",
    "STDOUT_LIMIT_ENV",
    "SUMMARY_PREVIEW_BYTES",
    "bound_output",
    "bound_traceback",
    "render_last_expression",
    "resolve_stdout_limit",
    "stdout_limit_for",
    "validate_display",
]
