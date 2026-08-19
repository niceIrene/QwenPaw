# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

import hashlib
import json
import time
from datetime import date, datetime, timezone
from typing import Any, AsyncGenerator, Literal
from uuid import uuid4

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage

from .buffer import _UsageEvent
from .manager import get_token_usage_manager


class TokenRecordingModelWrapper(ChatModelBase):
    """Wraps a ChatModelBase to record token usage on each call."""

    _usage_by_session: dict[str, dict[str, Any]] = {}
    _meta_dumped_sessions: set[str] = set()

    def __init__(
        self,
        provider_id: str,
        model: ChatModelBase,
        compact_threshold: float | None = None,
    ) -> None:
        # agentscope 2.0 ChatModelBase requires credential/model/parameters.
        # Forward the wrapped model's own values so the base attributes stay
        # consistent (some downstream code reads ``self.model`` for logging).
        super().__init__(
            credential=getattr(model, "credential", None),
            model=getattr(model, "model", "unknown"),
            parameters=getattr(model, "parameters", None)
            or ChatModelBase.Parameters(),
            stream=getattr(model, "stream", True),
            context_size=getattr(model, "context_size", 32768),
        )
        self._model = model
        self._provider_id = provider_id
        # Auto-compaction threshold (fraction of the window) for the UI, or
        # None when compaction is disabled/unknown.
        self._compact_threshold = compact_threshold

    def _record_usage(self, usage: ChatUsage | None) -> None:
        """Enqueue a usage event synchronously — never blocks the caller."""
        if usage is None:
            return
        pt = getattr(usage, "input_tokens", 0) or 0
        ct = getattr(usage, "output_tokens", 0) or 0
        if pt <= 0 and ct <= 0:
            return
        cache_read = getattr(usage, "cache_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        event = _UsageEvent(
            provider_id=self._provider_id,
            model_name=self.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            date_str=date.today().isoformat(),
            now_iso=datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds",
            ),
        )
        # Fire-and-forget: synchronous put_nowait, ~100 ns, no await needed.
        get_token_usage_manager().enqueue(event)

        usage_data = {
            "provider_id": self._provider_id,
            "model_name": self.model,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cache_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "total_tokens": pt + ct,
            # Context window of the wrapped model, so the UI can show how full
            # the *current* context is (prompt_tokens / context_size), distinct
            # from the cumulative session totals. 0 = unknown.
            "context_size": int(getattr(self._model, "context_size", 0) or 0),
            # Auto-compaction threshold (fraction of the window) so the UI can
            # mark where context gets evicted. None = disabled/unknown.
            "compact_threshold": self._compact_threshold,
        }
        self._store_usage(usage_data)
        self._append_jsonl(usage_data)

    def _append_jsonl(self, usage: dict[str, Any]) -> None:
        from ..app.agent_context import get_current_session_id
        from .sink import append_usage_record, configured_usage_jsonl_path

        path = configured_usage_jsonl_path()
        if path is None:
            return
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "session_id": get_current_session_id() or "",
            **usage,
        }
        record.pop("context_size", None)
        record.pop("compact_threshold", None)
        append_usage_record(path, record)

    @classmethod
    def pop_usage_for_session(cls, session_id: str) -> dict[str, Any] | None:
        cls._meta_dumped_sessions.discard(session_id)
        return cls._usage_by_session.pop(session_id, None)

    def _store_usage(self, usage: dict[str, Any] | None) -> None:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id()
        if session_id and usage:
            TokenRecordingModelWrapper._usage_by_session[session_id] = usage

    def _dump_request_meta(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> None:
        """Persist the real system prompt + tool schemas once per session.

        Benchmark adapters set ``QWENPAW_REQUEST_META_JSON`` to capture the
        exact payload sent to the model (for ATIF trajectory export) instead
        of reconstructing the prompt after the fact.
        """
        from ..app.agent_context import get_current_session_id
        from .sink import configured_request_meta_path, write_request_meta

        session_id = get_current_session_id() or ""
        if session_id in TokenRecordingModelWrapper._meta_dumped_sessions:
            return
        path = configured_request_meta_path()
        if path is None:
            return
        system_prompt = ""
        if messages:
            first = messages[0]
            if isinstance(first, dict):
                role = first.get("role")
                content = first.get("content")
            else:
                # agentscope passes Msg objects, not wire dicts.
                role = getattr(first, "role", None)
                get_text = getattr(first, "get_text_content", None)
                content = (
                    get_text()
                    if callable(get_text)
                    else getattr(first, "content", None)
                )
            if role == "system" and content is not None:
                if isinstance(content, str):
                    system_prompt = content
                else:
                    system_prompt = json.dumps(
                        content,
                        ensure_ascii=False,
                        default=str,
                    )
        write_request_meta(
            path,
            {
                "ts": datetime.now(tz=timezone.utc).isoformat(
                    timespec="seconds",
                ),
                "session_id": session_id,
                "model": self.model,
                "system_prompt": system_prompt,
                "tools": tools or [],
            },
        )
        TokenRecordingModelWrapper._meta_dumped_sessions.add(session_id)

    async def generate_structured_output(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = await self._model.generate_structured_output(*args, **kwargs)
        self._record_usage(getattr(result, "usage", None))
        return result

    def _emit_otel_span(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        usage: ChatUsage | None,
        started_iso: str,
        duration_ms: float,
    ) -> None:
        """Append one OTLP-style span per LLM call, gated by env.

        Hand-rolled (no opentelemetry SDK) so benchmark containers stay
        light; consumers read the JSONL directly or convert to OTLP.
        """
        from ..app.agent_context import get_current_session_id
        from .sink import append_usage_record, configured_otel_jsonl_path

        path = configured_otel_jsonl_path()
        if path is None:
            return
        session_id = get_current_session_id() or ""
        # Stable per-session trace id; random per-call span id.
        trace_id = hashlib.md5(session_id.encode("utf-8")).hexdigest()
        span: dict[str, Any] = {
            "name": f"qwenpaw.llm {self.model}",
            "trace_id": trace_id,
            "span_id": uuid4().hex[:16],
            "start_time": started_iso,
            "duration_ms": round(duration_ms, 3),
            "attributes": {
                "gen_ai.system": self._provider_id,
                "gen_ai.request.model": self.model,
                "session_id": session_id,
                "message_count": len(messages),
                "tool_count": len(tools or []),
            },
        }
        if usage is not None:
            attrs = span["attributes"]
            attrs["gen_ai.usage.input_tokens"] = (
                getattr(usage, "input_tokens", 0) or 0
            )
            attrs["gen_ai.usage.output_tokens"] = (
                getattr(usage, "output_tokens", 0) or 0
            )
        append_usage_record(path, span)

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # agentscope 2.0 routes structured output through
        # ``generate_structured_output`` instead of a ``__call__`` kwarg, and
        # provider SDKs (anthropic, openai) reject unknown kwargs. Drop the
        # 1.x ``structured_model`` if a caller still passes it.
        kwargs.pop("structured_model", None)

        self._dump_request_meta(messages, tools)

        # Fix: Omit tool_choice="auto" for vLLM compatibility
        # vLLM without --enable-auto-tool-choice will reject requests when
        # tool_choice="auto" is present, even if tools are provided.
        # By omitting tool_choice when it's "auto", we bypass the check
        # while keeping tools available for correct tool calling behavior.
        if tool_choice == "auto":
            tool_choice = None

        started_iso = datetime.now(tz=timezone.utc).isoformat(
            timespec="milliseconds",
        )
        started = time.monotonic()
        result = await self._model(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(
                result,
                messages=messages,
                tools=tools,
                started_iso=started_iso,
                started=started,
            )
        usage = getattr(result, "usage", None)
        self._record_usage(usage)
        self._emit_otel_span(
            messages=messages,
            tools=tools,
            usage=usage,
            started_iso=started_iso,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        *,
        messages: list[dict],
        tools: list[dict] | None,
        started_iso: str,
        started: float,
    ) -> AsyncGenerator[ChatResponse, None]:
        last_usage: ChatUsage | None = None
        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                last_usage = chunk.usage
            yield chunk
        self._record_usage(last_usage)
        self._emit_otel_span(
            messages=messages,
            tools=tools,
            usage=last_usage,
            started_iso=started_iso,
            duration_ms=(time.monotonic() - started) * 1000,
        )
