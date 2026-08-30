# -*- coding: utf-8 -*-
"""Kernel-side ``paw.lm`` proxies (paw.lm design doc §2.2, §3.1).

The orchestrator model writes Python that calls small-model inference as a
library function.  This module owns everything that happens *inside* the
sandboxed kernel: reference constructors (``var``/``file``/``history``),
kernel-side VarRef resolution (with spill-to-file on oversize values), and
payload assembly for the ``lm_call`` protocol message.  No model client code
lives here — the kernel has no network; inference is forwarded to the
main-process ``LMExecutor`` over the existing stdio channel.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: Vars larger than this spill to a workspace file and degrade to a FileRef
#: plus an explanatory note (design doc §2.2 — bounded, but explicit).
VAR_SPILL_BYTES = 24 * 1024
#: Kernel-side per-item inline cap; the executor enforces its own caps too.
INLINE_ITEM_BYTES = 32 * 1024

DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAP_CONCURRENCY = 8

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "violations": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "violations", "evidence"],
}

_EXTRACT_DEFAULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


class PawLMError(RuntimeError):
    """A ``paw.lm`` call failed; carries the structured error kind."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        code: str = "",
        retryable: bool = False,
        suggestion: str = "",
    ) -> None:
        self.kind = kind
        self.code = code or kind
        self.message = message
        self.retryable = retryable
        self.suggestion = suggestion
        text = f"{kind}: {message}"
        if suggestion:
            text = f"{text} {suggestion}"
        super().__init__(text)


class TaskResult(dict):
    """One sub-LM result: a plain dict with attribute access.

    Keys: ``status`` (``ok``/``unknown``/``error``), ``value`` (parsed per
    schema, or raw text when no schema), ``raw``, ``usage``.  Attribute
    lookup falls through to ``value`` keys so ``vr.passed`` works for
    ``paw.lm.verify`` results.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            value = self.get("value")
            if isinstance(value, Mapping) and name in value:
                return value[name]
            raise AttributeError(name) from None


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


class LMNamespace:
    """The ``paw.lm`` facade bound into the cell namespace."""

    def __init__(
        self,
        channel: Any,
        namespace: dict[str, Any],
        workspace: Path,
    ) -> None:
        self._channel = channel
        self._namespace = namespace
        self._workspace = workspace
        self._schemas: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------- context references
    @staticmethod
    def var(name: str) -> dict[str, Any]:
        """Reference a REPL variable by name (resolved at call time)."""
        return {"kind": "var", "name": str(name)}

    @staticmethod
    def file(
        path: str,
        *,
        start: int | None = None,
        end: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Reference a workspace file, optionally a 1-based line range."""
        ref: dict[str, Any] = {"kind": "file", "path": str(path)}
        if start is not None:
            ref["start"] = int(start)
        if end is not None:
            ref["end"] = int(end)
        if max_bytes is not None:
            ref["max_bytes"] = int(max_bytes)
        return ref

    @staticmethod
    def history(
        *,
        last_n: int | None = None,
        ids: Sequence[int] | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Reference a read-only slice of the scroll history store."""
        ref: dict[str, Any] = {"kind": "history"}
        if last_n is not None:
            ref["last_n"] = int(last_n)
        if ids is not None:
            ref["ids"] = [int(item) for item in ids]
        if query:
            ref["query"] = str(query)
        return ref

    # ------------------------------------------------------ named contracts
    def schema(
        self,
        name: str,
        definition: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Register a named JSON-schema contract reusable across cells.

        Registered names can be passed as ``schema="name"`` to
        ``call``/``map`` (and per-item overrides) instead of repeating the
        definition; telemetry aggregates validation failures per name.
        """
        key = str(name or "").strip()
        if not key:
            raise PawLMError(
                "validation_error",
                "paw.lm.schema requires a non-empty name",
            )
        if not isinstance(definition, Mapping) or not definition:
            raise PawLMError(
                "validation_error",
                "paw.lm.schema requires a non-empty JSON schema mapping",
            )
        self._schemas[key] = dict(definition)
        return {"name": key, "registered": True}

    def _resolve_schema(
        self,
        schema: Any,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve a schema argument to (definition, registered_name)."""
        if schema is None:
            return None, None
        if isinstance(schema, str):
            key = schema.strip()
            definition = self._schemas.get(key)
            if definition is None:
                available = sorted(self._schemas) or ["<none>"]
                raise PawLMError(
                    "validation_error",
                    f"unknown paw.lm schema {key!r}; registered: "
                    + ", ".join(available),
                )
            return dict(definition), key
        if isinstance(schema, Mapping):
            return dict(schema), None
        raise PawLMError(
            "validation_error",
            "schema must be a mapping or a registered name, got "
            f"{type(schema).__name__}",
        )

    # -------------------------------------------------------- primitives
    def call(
        self,
        task: str,
        context: Sequence[Any] = (),
        *,
        schema: Mapping[str, Any] | str | None = None,
        evidence: bool = False,
        model: str = "small",
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tools: Sequence[str] | None = None,
    ) -> TaskResult:
        """Run one constrained sub-LM call; returns a TaskResult."""
        if not isinstance(task, str) or not task.strip():
            raise PawLMError(
                "validation_error",
                "paw.lm.call requires a non-empty task string",
            )
        schema_def, schema_name = self._resolve_schema(schema)
        payload = {
            "op": "call",
            "task": task,
            "context": self._resolve_context(context),
            "schema": schema_def,
            "schema_name": schema_name,
            "evidence": bool(evidence),
            "model": str(model or "small"),
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "tools": [str(name) for name in tools] if tools else None,
        }
        return TaskResult(self._channel.call_lm(payload))

    def map(
        self,
        task: str | None = None,
        items: Sequence[Any] = (),
        context: Sequence[Any] = (),
        *,
        schema: Mapping[str, Any] | str | None = None,
        item_schema: Mapping[str, Any] | str | None = None,
        max_concurrency: int = DEFAULT_MAP_CONCURRENCY,
        on_error: str = "collect",
        evidence: bool = False,
        model: str = "small",
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tools: Sequence[str] | None = None,
    ) -> list[TaskResult]:
        """Fan out one batched sub-LM call per item (one channel round-trip).

        ``items`` are either bare payloads (rendered into the shared
        ``task``) or per-item override dicts with their own
        ``task``/``context``/``schema``/``temperature``/``max_tokens``
        (design doc §3.1, heterogeneous batches). ``item_schema`` declares
        the contract every raw item must satisfy; the executor validates
        all items before spending any tokens and reports bad indices.
        """
        if on_error not in {"collect", "fail_fast"}:
            raise PawLMError(
                "validation_error",
                f"on_error must be 'collect' or 'fail_fast', got {on_error!r}",
            )
        schema_def, schema_name = self._resolve_schema(schema)
        item_schema_def, item_schema_name = self._resolve_schema(item_schema)
        normalized = [
            self._normalize_item(item) for item in items
        ]
        payload = {
            "op": "map",
            "task": task,
            "items": normalized,
            "context": self._resolve_context(context),
            "schema": schema_def,
            "schema_name": schema_name,
            "item_schema": item_schema_def,
            "item_schema_name": item_schema_name,
            "max_concurrency": max(1, int(max_concurrency)),
            "on_error": on_error,
            "evidence": bool(evidence),
            "model": str(model or "small"),
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "tools": [str(name) for name in tools] if tools else None,
        }
        results = self._channel.call_lm(payload)
        return [TaskResult(item) for item in results]

    def extract(
        self,
        question: str,
        context: Sequence[Any] = (),
        *,
        schema: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Extract an answer from context; returns the parsed value."""
        result = self.call(
            question,
            context,
            schema=schema if schema is not None else _EXTRACT_DEFAULT_SCHEMA,
            **kwargs,
        )
        value = result.get("value")
        if schema is None and isinstance(value, Mapping):
            return value.get("answer")
        return value

    def summarize(
        self,
        context: Sequence[Any] = (),
        *,
        focus: str = "",
        max_length: int = 2000,
        **kwargs: Any,
    ) -> str:
        """Summarize context; returns plain text."""
        task = (
            "Summarize the context concisely"
            + (f", focusing on: {focus}" if focus else "")
            + f". Keep it under {int(max_length)} characters. "
            "Do not invent facts that are not in the context."
        )
        result = self.call(task, context, schema=None, **kwargs)
        value = result.get("value")
        return value if isinstance(value, str) else str(value)

    def verify(
        self,
        artifact: Any,
        spec: str,
        context: Sequence[Any] = (),
        **kwargs: Any,
    ) -> TaskResult:
        """Verify an artifact against a spec; returns passed/violations/evidence."""
        artifact_item = (
            artifact
            if isinstance(artifact, Mapping)
            else {"kind": "inline", "text": str(artifact)}
        )
        items = [self._resolve_one(self._with_label(artifact_item, "ARTIFACT"))]
        items.extend(self._resolve_context(context))
        return self._verify_call(items, spec, kwargs)

    def _verify_call(
        self,
        items: list[dict[str, Any]],
        spec: str,
        kwargs: dict[str, Any],
    ) -> TaskResult:
        payload = {
            "op": "call",
            "task": (
                "Verify the ARTIFACT below against this specification: "
                f"{spec}\nList every concrete violation; pass only when "
                "there are none."
            ),
            "context": items,
            "schema": dict(VERIFY_SCHEMA),
            "evidence": bool(kwargs.pop("evidence", False)),
            "model": str(kwargs.pop("model", "small")),
            "temperature": float(kwargs.pop("temperature", 0.0)),
            "max_tokens": int(kwargs.pop("max_tokens", DEFAULT_MAX_TOKENS)),
        }
        if kwargs:
            raise PawLMError(
                "validation_error",
                f"paw.lm.verify got unexpected arguments: {sorted(kwargs)}",
            )
        return TaskResult(self._channel.call_lm(payload))

    # -------------------------------------------------------- internals
    @staticmethod
    def _with_label(item: Mapping[str, Any], label: str) -> dict[str, Any]:
        merged = dict(item)
        merged["label"] = label
        return merged

    def _normalize_item(
        self,
        item: Any,
    ) -> dict[str, Any]:
        if isinstance(item, Mapping):
            normalized = dict(item)
            if "context" in normalized:
                normalized["context"] = self._resolve_context(
                    self._as_sequence(normalized.get("context")),
                )
            if "schema" in normalized:
                item_schema, item_schema_name = self._resolve_schema(
                    normalized.get("schema"),
                )
                normalized["schema"] = item_schema
                if item_schema_name:
                    normalized["schema_name"] = item_schema_name
            return normalized
        return {"payload": item}

    @staticmethod
    def _as_sequence(value: Any) -> Sequence[Any]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or isinstance(value, Mapping):
            return (value,)
        return tuple(value)

    def _resolve_context(self, context: Sequence[Any]) -> list[dict[str, Any]]:
        return [self._resolve_one(item) for item in self._as_sequence(context)]

    def _resolve_one(self, item: Any) -> dict[str, Any]:
        """Resolve one ContextItem; VarRefs are serialized kernel-side."""
        if isinstance(item, str):
            self._check_inline_bytes(item, "inline text")
            return {"kind": "inline", "text": item}
        if not isinstance(item, Mapping):
            return {
                "kind": "inline",
                "text": self._dumps(item),
                "label": type(item).__name__,
            }
        kind = str(item.get("kind") or "")
        if kind == "var":
            return self._resolve_var(str(item.get("name") or ""), item)
        if kind == "file":
            resolved = dict(item)
            resolved.setdefault("kind", "file")
            return resolved
        if kind == "history":
            return dict(item)
        if kind == "inline":
            text = str(item.get("text") or "")
            self._check_inline_bytes(text, "inline text")
            return dict(item)
        raise PawLMError(
            "validation_error",
            f"unknown context item kind: {kind!r}; use paw.lm.var/file/history "
            "or a plain string",
        )

    def _resolve_var(self, name: str, item: Mapping[str, Any]) -> dict[str, Any]:
        if not name or name not in self._namespace:
            raise PawLMError(
                "dangling_ref",
                f"paw.lm.var({name!r}) does not resolve: no such variable in "
                "the REPL namespace (it may have been lost to a kernel "
                "restart)",
                suggestion="Re-create the variable, or pass a file/history "
                "reference instead.",
            )
        value = self._namespace[name]
        text = self._dumps(value)
        label = str(item.get("label") or f"variable {name}")
        if _utf8_len(text) <= VAR_SPILL_BYTES:
            return {
                "kind": "var",
                "name": name,
                "value": text,
                "label": label,
            }
        spill = self._spill_var(name, text)
        note = (
            f"[paw.lm] variable {name!r} is {_utf8_len(text)} bytes; spilled "
            f"to {spill}. Only the first {VAR_SPILL_BYTES} bytes are "
            "attached; re-issue with paw.lm.file(path, start=..., end=...) "
            "for later slices."
        )
        return {
            "kind": "file",
            "path": spill,
            "max_bytes": VAR_SPILL_BYTES,
            "label": label,
            "note": note,
        }

    def _spill_var(self, name: str, text: str) -> str:
        out_dir = self._workspace / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        relative = Path("out") / f"lm_var_{name}_{uuid.uuid4().hex[:8]}.json"
        destination = self._workspace / relative
        destination.write_text(text, encoding="utf-8")
        return relative.as_posix()

    @staticmethod
    def _dumps(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str, indent=1)
        except (TypeError, ValueError):
            return repr(value)

    @staticmethod
    def _check_inline_bytes(text: str, what: str) -> None:
        size = _utf8_len(text)
        if size > INLINE_ITEM_BYTES:
            raise PawLMError(
                "context_too_large",
                f"{what} is {size} bytes; the per-item inline limit is "
                f"{INLINE_ITEM_BYTES}",
                suggestion="Assign it to a variable and pass paw.lm.var(name), "
                "or pass a paw.lm.file(...) slice.",
            )


__all__ = [
    "INLINE_ITEM_BYTES",
    "LMNamespace",
    "PawLMError",
    "TaskResult",
    "VAR_SPILL_BYTES",
    "VERIFY_SCHEMA",
]
