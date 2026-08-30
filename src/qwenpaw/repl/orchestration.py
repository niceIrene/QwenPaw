# -*- coding: utf-8 -*-
"""Orchestration mode configuration (design doc §9).

``QWENPAW_ORCHESTRATION=auto|forced`` swaps the model-facing ``repl_exec``
for ``orchestrate_python``: the same sandboxed kernel, but cell output is
starved to ~2 KB so bulk bytes can only reach the orchestrator through
``paw.lm`` results or mechanical in-kernel reduction. ``forced`` adds a
plan-first gate: the kernel rejects every cell until ``PLAN`` is defined.
"""

from __future__ import annotations

import os

ORCHESTRATION_STDOUT_LIMIT = 2048
PLAN_VARIABLE = "PLAN"

_MODES = {"auto", "forced"}


def orchestration_mode() -> str | None:
    """Return "auto"/"forced" when QWENPAW_ORCHESTRATION selects a mode."""
    mode = (os.getenv("QWENPAW_ORCHESTRATION") or "").strip().lower()
    return mode if mode in _MODES else None


def code_tool_name() -> str:
    """Model-facing name of the code-execution tool under current config."""
    return "orchestrate_python" if orchestration_mode() else "repl_exec"


__all__ = [
    "ORCHESTRATION_STDOUT_LIMIT",
    "PLAN_VARIABLE",
    "code_tool_name",
    "orchestration_mode",
]
