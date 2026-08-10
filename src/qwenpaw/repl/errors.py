# -*- coding: utf-8 -*-
"""Structured tool/execution errors for the CodeAct REPL (roadmap §2.4).

Every error surfaced to the model is rendered as one bounded JSON object::

    {
      "kind": "validation_error",
      "code": "missing_required_argument",
      "message": "Argument 'owner' is required.",
      "retryable": false,
      "suggestion": "Inspect the tool with help() or inspect.signature()."
    }

``kind`` drives the model's recovery strategy: fix the code, fix the
arguments, retry a bounded number of times, or stop and ask the user.
"""

from __future__ import annotations

from typing import Any

# Canonical error kinds.  ``failed`` is a catch-all for tool errors that do
# not map onto a more specific kind.
ERROR_KINDS = frozenset(
    {
        "syntax_error",
        "runtime_error",
        "tool_not_found",
        "validation_error",
        "permission_denied",
        "auth_missing",
        "rate_limited",
        "timeout",
        "interrupted",
        "kernel_restarted",
        "result_too_large",
        "budget_exhausted",
        "failed",
    },
)

#: Whether a kind is worth an automatic retry of (roughly) the same call.
RETRYABLE_KINDS = frozenset(
    {
        "syntax_error",
        "runtime_error",
        "rate_limited",
        "timeout",
        "interrupted",
        "kernel_restarted",
    },
)

#: Default, kind-specific recovery guidance shown to the model.
DEFAULT_SUGGESTIONS: dict[str, str] = {
    "syntax_error": "Fix the Python syntax and re-run the corrected cell.",
    "runtime_error": (
        "Inspect the traceback, fix the code, and retry. Use peek()/ls_vars() "
        "to check retained state."
    ),
    "tool_not_found": (
        "The tool is unavailable. Use paw.list_tools()/paw.search_tools() to "
        "discover current tools."
    ),
    "validation_error": (
        "Fix the arguments and retry. Inspect the tool with help() or "
        "inspect.signature()."
    ),
    "permission_denied": (
        "Policy denied this action. Do not try to bypass it; change the goal "
        "or ask the user."
    ),
    "auth_missing": (
        "Credentials are missing. Ask the user to authenticate; do not retry."
    ),
    "rate_limited": "Wait or reduce call frequency, then retry a limited number of times.",
    "timeout": "The call timed out. Reduce the workload or retry once.",
    "interrupted": "The cell was interrupted. Variables are retained; retry if appropriate.",
    "kernel_restarted": (
        "The kernel restarted and variables were lost. Re-run the required "
        "setup cells or restore persisted variables."
    ),
    "result_too_large": (
        "The result is too large to return. Keep it in a variable and inspect "
        "a bounded slice with peek()."
    ),
    "budget_exhausted": (
        "The cell budget is exhausted. Answer using retained results."
    ),
    "failed": "Inspect the error and retry only if the failure is transient.",
}


def make_error(
    kind: str,
    *,
    code: str = "",
    message: str = "",
    retryable: bool | None = None,
    suggestion: str | None = None,
) -> dict[str, Any]:
    """Build one structured error object with safe defaults."""
    resolved_kind = kind if kind in ERROR_KINDS else "failed"
    if retryable is None:
        retryable = resolved_kind in RETRYABLE_KINDS
    if suggestion is None:
        suggestion = DEFAULT_SUGGESTIONS.get(resolved_kind, "")
    return {
        "kind": resolved_kind,
        "code": str(code or resolved_kind),
        "message": str(message or ""),
        "retryable": bool(retryable),
        "suggestion": str(suggestion),
    }


def classify_exception(exc: BaseException) -> dict[str, Any]:
    """Map a Python exception raised inside a cell to a structured error."""
    import ast  # noqa: F401  (kept local to avoid import-time cost)

    from .proxy_runtime import PawToolError

    if isinstance(exc, PawToolError):
        kind = exc.kind if exc.kind in ERROR_KINDS else "failed"
        return make_error(
            kind,
            code=exc.kind,
            message=f"{exc.tool}: {exc.message}",
        )
    if isinstance(exc, (SyntaxError, IndentationError, TabError)):
        return make_error(
            "syntax_error",
            code=type(exc).__name__,
            message=str(exc),
        )
    if isinstance(exc, KeyboardInterrupt):
        return make_error("interrupted", message="cell interrupted")
    if isinstance(exc, (TimeoutError,)):
        return make_error("timeout", message=str(exc) or "timed out")
    if isinstance(exc, MemoryError):
        return make_error(
            "result_too_large",
            message="cell ran out of memory",
        )
    if isinstance(exc, NameError):
        return make_error(
            "runtime_error",
            code="NameError",
            message=str(exc),
        )
    return make_error(
        "runtime_error",
        code=type(exc).__name__,
        message=str(exc),
    )


def render_error_json(error: dict[str, Any]) -> str:
    """Serialize a structured error to a compact, deterministic JSON line."""
    import json

    ordered = {
        "kind": error.get("kind", "failed"),
        "code": error.get("code", ""),
        "message": error.get("message", ""),
        "retryable": bool(error.get("retryable", False)),
        "suggestion": error.get("suggestion", ""),
    }
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota",
    "throttl",
    "429",
    "503",
)

_AUTH_MARKERS = (
    "auth",
    "credential",
    "unauthorized",
    "api key",
    "api_key",
    "token expired",
    "permission scope",
    "login required",
    "401",
)


def classify_tool_error(kind: str, message: str) -> dict[str, Any]:
    """Map one governed tool-call failure to a structured error.

    ``kind`` is the GovernanceBridge token (``denied`` / ``failed`` /
    ``interrupted`` / a structured kind); the message is additionally
    scanned for rate-limit and auth markers so retryability is explicit.
    """
    text = str(message or "")
    lowered = text.lower()
    if kind == "denied":
        return make_error("permission_denied", message=text)
    if kind == "interrupted":
        return make_error("interrupted", message=text)
    if kind in ERROR_KINDS and kind not in {"denied", "failed"}:
        return make_error(kind, message=text)
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return make_error("rate_limited", message=text)
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return make_error("auth_missing", message=text, retryable=False)
    return make_error("failed", message=text, retryable=False)


__all__ = [
    "DEFAULT_SUGGESTIONS",
    "ERROR_KINDS",
    "RETRYABLE_KINDS",
    "classify_exception",
    "classify_tool_error",
    "make_error",
    "render_error_json",
]
