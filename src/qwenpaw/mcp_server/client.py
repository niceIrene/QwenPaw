# -*- coding: utf-8 -*-
"""HTTP client that proxies MCP tool calls into QwenPaw.

Every tool call becomes one ``POST`` to
``/api/agents/{agent_id}/console/chat`` with an SSE response. We consume
the stream, track the last ``message.completed`` (or ``response.completed``)
event, and return the assistant's final text. All failure modes resolve
to descriptive strings rather than raised exceptions, so Claude can show
the user an explanation instead of an opaque MCP protocol error.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_EXT_TO_SOURCE_TYPE = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".txt": "text",
    ".md": "text",
}


@dataclass(frozen=True)
class ClientConfig:
    """Immutable runtime config for the HTTP proxy."""

    base_url: str
    agent_id: str
    user_id: str
    timeout: float


async def send_message(
    text: str,
    config: ClientConfig,
    session_id: str,
    progress_cb: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Forward ``text`` to QwenPaw's console channel and return the reply."""
    payload = {
        "channel": "console",
        "user_id": config.user_id,
        "session_id": session_id,
        "input": [
            {"content": [{"type": "text", "text": text}]},
        ],
    }
    url = f"{config.base_url.rstrip('/')}/api/agents/{config.agent_id}" \
        "/console/chat"

    try:
        async with httpx.AsyncClient(timeout=config.timeout) as c:
            async with c.stream("POST", url, json=payload) as resp:
                if resp.status_code == 404:
                    return (
                        f"Agent '{config.agent_id}' not found. "
                        "Check --agent-id."
                    )
                if resp.status_code == 503:
                    return (
                        f"Console channel not registered on agent "
                        f"'{config.agent_id}'."
                    )
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    return (
                        f"QwenPaw returned HTTP {resp.status_code}: "
                        f"{body[:200]}"
                    )
                return await _consume_sse(resp, progress_cb)
    except httpx.ConnectError:
        return (
            f"Cannot reach QwenPaw at {config.base_url}. "
            "Is 'copaw app' running?"
        )
    except httpx.TimeoutException:
        return (
            f"QwenPaw did not finish within {int(config.timeout)}s. "
            "Long ingests may still be running — check the QwenPaw console."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error talking to QwenPaw")
        return f"QwenPaw proxy error: {exc}"


async def ingest_file(
    path: str,
    title: str | None,
    config: ClientConfig,
    session_id: str,
) -> str:
    """Hand a local file to Copilot Digest's ingest pipeline by path."""
    abs_path = Path(path).expanduser()
    try:
        abs_path = abs_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return f"File not found / not readable: {path}"

    if not abs_path.is_file() or not os.access(abs_path, os.R_OK):
        return f"File not found / not readable: {path}"

    suffix = abs_path.suffix.lower()
    source_type = _EXT_TO_SOURCE_TYPE.get(suffix)
    if source_type is None:
        return (
            f"Unsupported file type '{suffix}'. Copilot Digest handles "
            "PDF, CSV, TXT, and MD."
        )

    prompt_lines = [
        f"Save this file to my reading list: {abs_path}",
        f"Source type: {source_type}",
    ]
    if title:
        prompt_lines.append(f"Title: {title}")
    return await send_message("\n".join(prompt_lines), config, session_id)


async def _consume_sse(
    resp: httpx.Response,
    progress_cb: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Walk SSE frames; return the last completed assistant message text."""
    last_text = ""
    saw_first_event = False
    async for raw_line in resp.aiter_lines():
        if not raw_line or not raw_line.startswith("data:"):
            continue
        payload = raw_line[5:].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON SSE frame: %.120s", payload)
            continue

        if not saw_first_event:
            logger.debug("First SSE frame from QwenPaw: %s", event)
            saw_first_event = True
            if progress_cb:
                await progress_cb("Assistant is processing...")

        if isinstance(event, dict) and "error" in event and len(event) == 1:
            return f"QwenPaw error: {event['error']}"

        terminal = _extract_terminal_text(event)
        if terminal:
            last_text = terminal

    return last_text or "(assistant produced no text response)"


def _extract_terminal_text(event: Any) -> str:
    """Pull assistant text out of a ``*.completed`` event, if this is one."""
    if not isinstance(event, dict):
        return ""
    if event.get("status") != "completed":
        return ""
    obj = event.get("object")
    if obj not in ("message", "response"):
        return ""

    # Agentscope-runtime event schema: content is a list of parts; text
    # parts have {"type": "text", "text": "..."}. Exact nesting varies by
    # event type (`message` vs `response`), so probe both shapes.
    chunks: list[str] = []
    for container in (event, event.get("message"), event.get("response")):
        if not isinstance(container, dict):
            continue
        parts = container.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks)
