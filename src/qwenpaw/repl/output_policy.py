# -*- coding: utf-8 -*-
"""Bounded REPL output and spill files (design doc §2.4 and §2.9-D2)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_STDOUT_LIMIT = 2048
DEFAULT_TRACEBACK_LIMIT = 16 * 1024

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
        "display must be one of none|summary|full, got {display!r}".format(
            display=display,
        ),
    )


def render_last_expression(value: Any, display: str = DEFAULT_DISPLAY) -> str | None:
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
            "output path must be relative and cannot contain '..'"
        )
    root = workspace.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("output path escapes the REPL workspace")
    return resolved


def bound_output(
    text: str,
    *,
    workspace: Path,
    spill_name: str,
    limit: int = DEFAULT_STDOUT_LIMIT,
) -> BoundedOutput:
    """Return bounded text, spilling the full UTF-8 payload when necessary."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return BoundedOutput(text=text)

    relative = Path("out") / spill_name
    destination = _safe_workspace_path(workspace, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)

    head_lines = text.splitlines(keepends=True)[:8]
    head = "".join(head_lines)
    summary = (
        f"[output {len(encoded)} bytes saved to {relative.as_posix()}; "
        "first 8 lines follow. The full spill is archival: keep using the "
        "original REPL variables, or inspect a bounded slice with "
        f"peek_file({relative.as_posix()!r}); do not print the whole spill.]\n"
        f"{head}"
    )
    # A single pathological first line must not defeat the hard context cap.
    summary_bytes = summary.encode("utf-8", errors="replace")
    if len(summary_bytes) > limit:
        summary = summary_bytes[:limit].decode("utf-8", errors="ignore")
    return BoundedOutput(text=summary, spilled=(relative.as_posix(),))


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


def safe_save(
    obj: object,
    relpath: str,
    *,
    workspace: Path,
) -> str:
    """Persist JSON-compatible data or text inside the workspace."""
    import json

    destination = _safe_workspace_path(workspace, relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, str):
        destination.write_text(obj, encoding="utf-8")
    elif isinstance(obj, bytes):
        destination.write_bytes(obj)
    else:
        destination.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return os.path.relpath(destination, workspace)


__all__ = [
    "BoundedOutput",
    "DEFAULT_DISPLAY",
    "DEFAULT_STDOUT_LIMIT",
    "DEFAULT_TRACEBACK_LIMIT",
    "DISPLAY_MODES",
    "SUMMARY_PREVIEW_BYTES",
    "bound_output",
    "bound_traceback",
    "render_last_expression",
    "safe_save",
    "validate_display",
]
