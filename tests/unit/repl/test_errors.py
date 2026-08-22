"""Structured error taxonomy and classification (roadmap §2.4)."""

from __future__ import annotations

import json

import pytest

from qwenpaw.repl.errors import (
    DEFAULT_SUGGESTIONS,
    ERROR_KINDS,
    RETRYABLE_KINDS,
    classify_exception,
    classify_tool_error,
    make_error,
    render_error_json,
)


class TestMakeError:
    def test_required_kinds_exist(self) -> None:
        required = {
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
        }
        assert required <= ERROR_KINDS

    def test_defaults(self) -> None:
        error = make_error("validation_error", message="bad arg")
        assert error["kind"] == "validation_error"
        assert error["code"] == "validation_error"
        assert error["message"] == "bad arg"
        assert error["retryable"] is False
        assert error["suggestion"] == DEFAULT_SUGGESTIONS["validation_error"]

    def test_retryable_default_follows_kind(self) -> None:
        assert make_error("rate_limited")["retryable"] is True
        assert make_error("permission_denied")["retryable"] is False
        for kind in RETRYABLE_KINDS:
            assert make_error(kind)["retryable"] is True

    def test_unknown_kind_degrades_to_failed(self) -> None:
        error = make_error("something_new")
        assert error["kind"] == "failed"

    def test_explicit_overrides(self) -> None:
        error = make_error(
            "timeout",
            code="custom",
            retryable=False,
            suggestion="stop",
        )
        assert error["code"] == "custom"
        assert error["retryable"] is False
        assert error["suggestion"] == "stop"


class TestRenderErrorJson:
    def test_compact_deterministic_json(self) -> None:
        error = make_error("syntax_error", message="oops")
        rendered = render_error_json(error)
        parsed = json.loads(rendered)
        assert list(parsed.keys()) == [
            "kind",
            "code",
            "message",
            "retryable",
            "suggestion",
        ]
        assert parsed["kind"] == "syntax_error"

    def test_missing_fields_get_safe_defaults(self) -> None:
        parsed = json.loads(render_error_json({"kind": "weird"}))
        assert parsed["kind"] == "weird"
        assert parsed["retryable"] is False


class TestClassifyException:
    def test_syntax_error(self) -> None:
        error = classify_exception(SyntaxError("bad"))
        assert error["kind"] == "syntax_error"

    def test_keyboard_interrupt(self) -> None:
        error = classify_exception(KeyboardInterrupt())
        assert error["kind"] == "interrupted"
        assert error["retryable"] is True

    def test_timeout(self) -> None:
        assert classify_exception(TimeoutError())["kind"] == "timeout"

    def test_memory_error_maps_to_result_too_large(self) -> None:
        assert classify_exception(MemoryError())["kind"] == "result_too_large"

    def test_name_error_is_runtime(self) -> None:
        error = classify_exception(NameError("name 'x' is not defined"))
        assert error["kind"] == "runtime_error"
        assert error["code"] == "NameError"

    def test_generic_exception_is_runtime(self) -> None:
        error = classify_exception(ValueError("bad"))
        assert error["kind"] == "runtime_error"
        assert error["code"] == "ValueError"

    def test_paw_tool_error_keeps_kind(self) -> None:
        from qwenpaw.repl.proxy_runtime import PawToolError

        error = classify_exception(
            PawToolError("permission_denied", "tool", "blocked"),
        )
        assert error["kind"] == "permission_denied"
        assert "tool" in error["message"]


class TestClassifyToolError:
    def test_denied_maps_to_permission_denied(self) -> None:
        error = classify_tool_error("denied", "policy said no")
        assert error["kind"] == "permission_denied"
        assert error["retryable"] is False

    def test_interrupted_passthrough(self) -> None:
        assert classify_tool_error("interrupted", "x")["kind"] == "interrupted"

    def test_known_kind_passthrough(self) -> None:
        assert classify_tool_error("timeout", "slow")["kind"] == "timeout"

    def test_rate_limit_heuristic(self) -> None:
        error = classify_tool_error("failed", "HTTP 429 Too Many Requests")
        assert error["kind"] == "rate_limited"
        assert error["retryable"] is True

    def test_auth_heuristic(self) -> None:
        error = classify_tool_error("failed", "401 Unauthorized: token expired")
        assert error["kind"] == "auth_missing"
        assert error["retryable"] is False

    def test_plain_failure_is_not_retryable(self) -> None:
        error = classify_tool_error("failed", "disk full")
        assert error["kind"] == "failed"
        assert error["retryable"] is False

    @pytest.mark.parametrize("kind", sorted(ERROR_KINDS - {"failed"}))
    def test_every_kind_is_constructible(self, kind: str) -> None:
        assert make_error(kind)["kind"] == kind
