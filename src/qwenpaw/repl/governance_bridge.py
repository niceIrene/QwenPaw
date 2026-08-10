# -*- coding: utf-8 -*-
"""Main-process governed tool forwarding (design doc §2.6 and §2.8).

This module deliberately does not execute any concrete tool itself. Every
kernel request is converted back into an AgentScope ``ToolCallBlock`` and sent
through the current ``Toolkit.call_tool`` entry point.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import mimetypes
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .proxy_runtime import sanitize_name

EXCLUDED_TOOLS = frozenset(
    {"repl_exec", "execute_python", "execute_python_code"}
)


@dataclass(frozen=True)
class CodeProvenance:
    """Metadata attached to governance calls originating inside a REPL cell."""

    workspace_id: str
    kernel_task_id: str
    provenance: str = "code"


_CURRENT_PROVENANCE: contextvars.ContextVar[CodeProvenance | None] = (
    contextvars.ContextVar("qwenpaw_repl_provenance", default=None)
)


def get_code_provenance() -> CodeProvenance | None:
    """Return metadata for the current code-originated nested tool call."""
    return _CURRENT_PROVENANCE.get()


@contextlib.contextmanager
def code_provenance(
    workspace_id: str,
    kernel_task_id: str,
) -> Iterator[CodeProvenance]:
    """Set code-call provenance for the normal governance pipeline."""
    value = CodeProvenance(
        workspace_id=workspace_id,
        kernel_task_id=kernel_task_id,
    )
    token = _CURRENT_PROVENANCE.set(value)
    try:
        yield value
    finally:
        _CURRENT_PROVENANCE.reset(token)


class ToolForwardingError(RuntimeError):
    """Raised for name collisions or unavailable forwarded tools."""


def _tool_objects(toolkit: Any) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for group in getattr(toolkit, "tool_groups", ()) or ():
        for tool in getattr(group, "tools", ()) or ():
            name = getattr(tool, "name", None)
            if isinstance(name, str) and name:
                tools[name] = tool
    return tools


def _descriptor_of(tool: Any) -> Any | None:
    """Best-effort ToolDescriptor lookup on a (possibly wrapped) tool."""
    for candidate in (
        tool,
        getattr(tool, "func", None),
        getattr(tool, "_func", None),
    ):
        descriptor = getattr(candidate, "_tool_descriptor", None)
        if descriptor is not None:
            return descriptor
    return None


def _mcp_path(tool: Any, fallback_name: str) -> str | None:
    capability = getattr(tool, "_capability", None)
    if capability is not None and getattr(capability, "protocol", "") == "mcp":
        server = sanitize_name(str(getattr(capability, "driver_name", "mcp")))
        original = sanitize_name(
            str(getattr(capability, "name", fallback_name))
        )
        return f"mcp.{server}.{original}"
    if bool(getattr(tool, "is_mcp", False)):
        server = sanitize_name(str(getattr(tool, "mcp_name", "mcp")))
        return f"mcp.{server}.{sanitize_name(fallback_name)}"
    return None


async def build_tool_specs(
    toolkit: Any,
    agent_state: Any,
) -> list[dict[str, Any]]:
    """Build stable proxy metadata from the current Toolkit schemas."""
    groups = getattr(
        getattr(agent_state, "tool_context", None),
        "activated_groups",
        None,
    )
    schemas = await toolkit.get_tool_schemas(groups)
    objects = _tool_objects(toolkit)
    specs: list[dict[str, Any]] = []
    by_path: dict[str, str] = {}
    for schema in schemas:
        function = (
            schema.get("function") if isinstance(schema, Mapping) else None
        )
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "")
        if not name or name in EXCLUDED_TOOLS:
            continue
        tool = objects.get(name)
        path = _mcp_path(tool, name) if tool is not None else None
        path = path or sanitize_name(name)
        existing = by_path.get(path)
        if existing is not None and existing != name:
            raise ToolForwardingError(
                "tool name sanitization collision: "
                f"{existing!r} and {name!r} both map to paw.tools.{path}",
            )
        by_path[path] = name
        parameters = function.get("parameters")
        specs.append(
            {
                "path": path,
                "name": name,
                "description": str(function.get("description") or ""),
                "schema": (
                    dict(parameters)
                    if isinstance(parameters, Mapping)
                    else {"type": "object", "properties": {}}
                ),
            },
        )
    return specs


def _block_attr(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, Mapping):
        return block.get(name, default)
    return getattr(block, name, default)


def _state_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _text_value(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _media_extension(media_type: str) -> str:
    return mimetypes.guess_extension(media_type, strict=False) or ".bin"


class GovernanceBridge:
    """Forward kernel calls through the current Toolkit and normal guards."""

    def __init__(
        self,
        toolkit: Any,
        agent_state: Any,
        workspace: Path,
        workspace_id: str,
        specs: list[Mapping[str, Any]],
    ) -> None:
        self.toolkit = toolkit
        self.agent_state = agent_state
        self.workspace = workspace.resolve()
        self.workspace_id = workspace_id
        self.specs = [dict(item) for item in specs]
        self._real_names = {
            str(item["path"]): str(item["name"])
            for item in self.specs
            if item.get("path") and item.get("name")
        }
        self._read_only_paths = self._collect_read_only_paths()

    def _collect_read_only_paths(self) -> frozenset[str]:
        """Paths explicitly marked read-only by their ToolDescriptor.

        Only an explicit ``governance.read_only=True`` (or descriptor
        ``metadata.read_only``) opts a tool into bounded concurrent
        forwarding; everything else stays strictly serial (roadmap P2).
        """
        objects = _tool_objects(self.toolkit)
        read_only: set[str] = set()
        for path, name in self._real_names.items():
            tool = objects.get(name)
            descriptor = _descriptor_of(tool)
            if descriptor is None:
                continue
            governance = getattr(descriptor, "governance", None)
            metadata = getattr(descriptor, "metadata", None) or {}
            if bool(
                getattr(governance, "read_only", False),
            ) or bool(metadata.get("read_only", False)):
                read_only.add(path)
        return frozenset(read_only)

    def is_read_only(self, exposed_path: str) -> bool:
        """Whether this tool was explicitly declared read-only."""
        return exposed_path in self._read_only_paths

    @classmethod
    async def from_current_context(
        cls,
        *,
        workspace: Path,
        workspace_id: str,
        toolkit: Any = None,
        agent_state: Any = None,
    ) -> "GovernanceBridge":
        """Create a bridge from bound or ContextVar runtime dependencies."""
        from ..config.context import (
            get_current_agent_state,
            get_current_toolkit,
        )

        if toolkit is None:
            toolkit = get_current_toolkit()
        if agent_state is None:
            agent_state = get_current_agent_state()
        if toolkit is None or agent_state is None:
            raise ToolForwardingError(
                "repl_exec requires an active QwenPaw Toolkit and AgentState",
            )
        specs = await build_tool_specs(toolkit, agent_state)
        return cls(toolkit, agent_state, workspace, workspace_id, specs)

    def _resolve_tool(self, exposed_path: str) -> str:
        if exposed_path in {sanitize_name(item) for item in EXCLUDED_TOOLS}:
            raise ToolForwardingError(
                f"{exposed_path} is not available inside repl_exec; "
                f"use the structured tool `{exposed_path}` directly",
            )
        name = self._real_names.get(exposed_path)
        if name is None:
            raise ToolForwardingError(
                f"tool paw.tools.{exposed_path} is unavailable; "
                "use dir(paw.tools) and help(...) to inspect current tools",
            )
        return name

    def _save_media(
        self, block: Any, call_id: str, position: int
    ) -> dict[str, str]:
        source = _block_attr(block, "source")
        media_type = str(
            _block_attr(source, "media_type", "application/octet-stream")
        )
        data = _block_attr(source, "data")
        if not isinstance(data, str):
            raise ToolForwardingError(
                "binary tool result did not contain base64 data"
            )
        relative = Path("out") / (
            f"tool_{call_id}_{position}{_media_extension(media_type)}"
        )
        destination = (self.workspace / relative).resolve()
        if self.workspace not in destination.parents:
            raise ToolForwardingError(
                "media result path escaped the workspace"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(data, validate=True))
        return {"path": relative.as_posix(), "media_type": media_type}

    def _convert_content(self, content: list[Any], call_id: str) -> Any:
        values: list[Any] = []
        for position, block in enumerate(content):
            block_type = str(_block_attr(block, "type", ""))
            if block_type == "text":
                values.append(_text_value(str(_block_attr(block, "text", ""))))
                continue
            source = _block_attr(block, "source")
            if source is not None and _block_attr(source, "data") is not None:
                values.append(self._save_media(block, call_id, position))
                continue
            values.append(repr(block))
        if not values:
            return None
        return values[0] if len(values) == 1 else values

    async def dispatch(
        self,
        exposed_path: str,
        args: dict[str, Any],
        *,
        kernel_task_id: str,
    ) -> Any:
        """Dispatch one request through ``Toolkit.call_tool``."""
        from agentscope.message import ToolCallBlock

        tool_name = self._resolve_tool(exposed_path)
        call_id = f"repl_{uuid.uuid4().hex[:12]}"
        tool_call = ToolCallBlock(
            id=call_id,
            name=tool_name,
            input=json.dumps(args, ensure_ascii=False),
        )
        response: Any = None
        with code_provenance(self.workspace_id, kernel_task_id):
            stream = self.toolkit.call_tool(tool_call, self.agent_state)
            async for chunk in stream:
                response = chunk

        if response is None:
            raise ToolForwardingError(f"tool {tool_name} returned no response")
        state = _state_value(getattr(response, "state", None))
        content = list(getattr(response, "content", None) or [])
        value = self._convert_content(content, call_id)
        if state in {"error", "denied", "interrupted"}:
            if state == "denied":
                kind = "denied"
            elif state == "interrupted":
                kind = "interrupted"
            else:
                kind = "failed"
            message = str(value or f"tool ended with state={state}")
            suggestion = (
                "Policy denied this action; changing parameters to bypass the "
                "policy is not allowed."
                if kind == "denied"
                else (
                    "The call was interrupted; retry only if still needed."
                    if kind == "interrupted"
                    else (
                        "Inspect the error and retry only if the failure is "
                        "transient."
                    )
                )
            )
            raise ToolForwardingError(f"{kind}|{message} {suggestion}")
        # Successful side-effect tools must return an explicit receipt.
        if value is None or value == "":
            return {"ok": True, "tool": tool_name}
        return value


__all__ = [
    "CodeProvenance",
    "EXCLUDED_TOOLS",
    "GovernanceBridge",
    "ToolForwardingError",
    "build_tool_specs",
    "code_provenance",
    "get_code_provenance",
]
