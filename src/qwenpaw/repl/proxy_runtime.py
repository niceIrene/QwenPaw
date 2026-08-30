# -*- coding: utf-8 -*-
"""Kernel-side ``paw.tools`` proxies and helpers (design doc §2.3, §2.8)."""

from __future__ import annotations

import collections
import inspect
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Protocol

from .daemon import (
    daemon_log,
    daemon_status,
    start_daemon as daemon_start,
)
from .lm_runtime import LMNamespace
from .output_policy import _safe_workspace_path

CELL_MODULE_NAME = "__qwenpaw_repl__"


class PawToolError(RuntimeError):
    """A governed tool call failed or was denied in the QwenPaw process."""

    def __init__(self, kind: str, tool: str, message: str) -> None:
        self.kind = kind
        self.tool = tool
        self.message = message
        super().__init__(f"{kind}: {tool}: {message}")


class KernelChannel(Protocol):
    """Minimal transport used by generated tool proxies."""

    def call_tool(self, tool: str, args: dict[str, Any]) -> Any:
        """Send a tool call and synchronously wait for its result."""

    def call_lm(self, payload: dict[str, Any]) -> Any:
        """Send one ``lm_call`` payload and wait for its ``lm_result``."""


def sanitize_name(name: str) -> str:
    """Map an arbitrary tool path segment to a valid Python identifier."""
    sanitized = re.sub(r"[^0-9A-Za-z_]", "_", str(name))
    if not sanitized:
        sanitized = "_"
    if sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized


def _schema_annotation(schema: Mapping[str, Any]) -> Any:
    annotations = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return annotations.get(str(schema.get("type")), Any)


def signature_from_schema(schema: Mapping[str, Any]) -> inspect.Signature:
    """Build a typed Python signature from a JSON object schema."""
    properties = schema.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    required = set(schema.get("required") or ())
    parameters: list[inspect.Parameter] = []
    optional_started = False
    for raw_name, raw_schema in props.items():
        name = sanitize_name(str(raw_name))
        item_schema = raw_schema if isinstance(raw_schema, Mapping) else {}
        is_required = raw_name in required and "default" not in item_schema
        default: Any = inspect.Parameter.empty
        if not is_required:
            optional_started = True
            default = item_schema.get("default", None)
        kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
        # JSON schema property order is not guaranteed to place required
        # arguments first. Keyword-only parameters preserve every name without
        # creating an invalid Python signature.
        if optional_started and is_required:
            kind = inspect.Parameter.KEYWORD_ONLY
        parameters.append(
            inspect.Parameter(
                name,
                kind,
                default=default,
                annotation=_schema_annotation(item_schema),
            ),
        )
    return inspect.Signature(parameters=parameters, return_annotation=Any)


def _example_from_schema(path: str, schema: Mapping[str, Any]) -> str:
    properties = schema.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    required = set(schema.get("required") or ())
    args: list[str] = []
    for name, item in props.items():
        if name not in required:
            continue
        item_schema = item if isinstance(item, Mapping) else {}
        example = item_schema.get("example", item_schema.get("default"))
        if example is None:
            annotation = _schema_annotation(item_schema)
            example = {
                str: "value",
                int: 1,
                float: 1.0,
                bool: True,
                list: [],
                dict: {},
            }.get(annotation, None)
        args.append(f"{sanitize_name(str(name))}={example!r}")
    return f"paw.tools.{path}({', '.join(args)})"


class ToolCallable:
    """Callable proxy with a schema-derived signature and docstring."""

    def __init__(
        self,
        path: str,
        spec: Mapping[str, Any] | None,
        channel: KernelChannel,
    ) -> None:
        self._path = path
        self._channel = channel
        schema = (
            spec.get("schema", {})
            if isinstance(spec, Mapping)
            else {"type": "object", "properties": {}}
        )
        self.__signature__ = signature_from_schema(schema)
        description = (
            str(spec.get("description") or "")
            if isinstance(spec, Mapping)
            else ""
        )
        example = _example_from_schema(path, schema)
        self.__doc__ = (f"{description}\n\nExample:\n    {example}").strip()
        self.__name__ = path.rsplit(".", 1)[-1]

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        bound = self.__signature__.bind(*args, **kwargs)
        return self._channel.call_tool(self._path, dict(bound.arguments))

    def __repr__(self) -> str:
        return f"<paw tool {self._path}{self.__signature__}>"


class ToolNamespace:
    """Dynamic, discoverable namespace for built-in and MCP tool proxies."""

    def __init__(
        self,
        channel: KernelChannel,
        specs: list[Mapping[str, Any]],
        prefix: tuple[str, ...] = (),
    ) -> None:
        self._channel = channel
        self._prefix = prefix
        self.update(specs)

    def update(self, specs: list[Mapping[str, Any]]) -> None:
        self._specs = {
            str(item.get("path")): dict(item)
            for item in specs
            if isinstance(item, Mapping) and item.get("path")
        }

    def _full_path(self, name: str) -> str:
        return ".".join((*self._prefix, sanitize_name(name)))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        path = self._full_path(name)
        if path in self._specs:
            return ToolCallable(path, self._specs[path], self._channel)
        prefix = path + "."
        if any(item.startswith(prefix) for item in self._specs):
            return ToolNamespace(
                self._channel,
                list(self._specs.values()),
                (*self._prefix, sanitize_name(name)),
            )
        # Tool lists can change while a task is running. Unknown paths remain
        # callable and are resolved authoritatively in the main process.
        return ToolCallable(path, None, self._channel)

    def __dir__(self) -> list[str]:
        prefix = ".".join(self._prefix)
        dotted_prefix = prefix + "." if prefix else ""
        children: set[str] = set()
        for path in self._specs:
            if not path.startswith(dotted_prefix):
                continue
            remainder = path[len(dotted_prefix) :]
            if remainder:
                children.add(remainder.split(".", 1)[0])
        return sorted(children)

    def __repr__(self) -> str:
        path = ".".join(self._prefix) or "paw.tools"
        return f"<tool namespace {path}: {', '.join(dir(self))}>"

    # ---------------------------------------------------- discovery (§2.9)
    def list_tools(self) -> list[dict[str, Any]]:
        """Return bounded metadata for every tool known to this kernel."""
        return [
            {
                "path": str(item.get("path")),
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or "")[:240],
            }
            for item in sorted(
                self._specs.values(),
                key=lambda spec: str(spec.get("path") or ""),
            )
        ]

    def search_tools(self, query: Any) -> list[dict[str, Any]]:
        """Case-insensitive substring/token search over known tool specs.

        Every whitespace-separated token must appear in the tool's path,
        name, or description for the tool to match.
        """
        tokens = [token.lower() for token in str(query or "").split() if token]
        if not tokens:
            return []
        matches: list[dict[str, Any]] = []
        for item in self.list_tools():
            haystack = " ".join(
                (item["path"], item["name"], item["description"]),
            ).lower()
            if all(token in haystack for token in tokens):
                matches.append(item)
        return matches

    def describe_tool(self, path: Any) -> dict[str, Any] | None:
        """Return the full known spec for one tool path, or ``None``."""
        spec = self._specs.get(str(path))
        if spec is None:
            return None
        return {
            "path": str(spec.get("path")),
            "name": str(spec.get("name") or ""),
            "description": str(spec.get("description") or ""),
            "schema": dict(spec.get("schema") or {}),
        }


def build_namespace(
    channel: KernelChannel,
    workspace: Path,
    tools: list[Mapping[str, Any]],
) -> tuple[dict[str, Any], Callable[[list[Mapping[str, Any]]], None]]:
    """Build the persistent user namespace and its tool-list updater."""
    # The namespace is the __dict__ of a real module registered in
    # sys.modules.  Cell-defined functions/classes therefore pickle by
    # reference: fork-based multiprocessing children inherit sys.modules
    # and can resolve ``__qwenpaw_repl__`` while unpickling callables
    # (spawn/forkserver still cannot import it — fork only).
    cell_module = sys.modules.get(CELL_MODULE_NAME)
    if cell_module is None:
        cell_module = ModuleType(CELL_MODULE_NAME)
        sys.modules[CELL_MODULE_NAME] = cell_module
    namespace: dict[str, Any] = cell_module.__dict__
    namespace["__name__"] = CELL_MODULE_NAME
    namespace["json"] = json
    namespace["pathlib"] = __import__("pathlib")
    namespace["collections"] = collections
    namespace["re"] = re
    try:
        namespace["pandas"] = __import__("pandas")
        namespace["pd"] = namespace["pandas"]
    except ImportError:
        pass

    tool_namespace = ToolNamespace(channel, tools)
    namespace["paw"] = SimpleNamespace(
        tools=tool_namespace,
        list_tools=tool_namespace.list_tools,
        search_tools=tool_namespace.search_tools,
        describe_tool=tool_namespace.describe_tool,
        daemon=lambda command, name: daemon_start(command, name, workspace),
        daemon_status=lambda name: daemon_status(name, workspace),
        daemon_log=lambda name, max_bytes=2000: daemon_log(
            name,
            workspace,
            max_bytes,
        ),
        # Bound to the live cell module dict: VarRef resolution always sees
        # the variables as they are at call time (paw.lm design doc §2.2).
        lm=LMNamespace(channel, namespace, workspace),
    )
    namespace["workspace"] = workspace.resolve()

    def workspace_path(relpath: str = ".") -> Path:
        """Return a sandbox-safe absolute path below the active workspace."""

        return _safe_workspace_path(workspace, relpath)

    namespace["workspace_path"] = workspace_path

    internal_names = frozenset(namespace)

    def update_tools(updated: list[Mapping[str, Any]]) -> None:
        tool_namespace.update(updated)

    namespace["_paw_internal_names"] = internal_names | {
        "workspace",
        "workspace_path",
        "_paw_internal_names",
    }
    return namespace, update_tools


def new_tool_call_id() -> str:
    """Return a compact unique id for one kernel-to-main tool call."""
    return f"t-{uuid.uuid4().hex[:12]}"


__all__ = [
    "KernelChannel",
    "PawToolError",
    "ToolCallable",
    "ToolNamespace",
    "build_namespace",
    "new_tool_call_id",
    "sanitize_name",
    "signature_from_schema",
]
