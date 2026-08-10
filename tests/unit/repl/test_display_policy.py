"""Display policy for the final cell expression (roadmap §2.3)."""

from __future__ import annotations

import pytest

from qwenpaw.repl.output_policy import (
    DEFAULT_DISPLAY,
    DISPLAY_MODES,
    SUMMARY_PREVIEW_BYTES,
    render_last_expression,
    validate_display,
)


class TestValidateDisplay:
    def test_accepts_documented_modes(self) -> None:
        assert set(DISPLAY_MODES) == {"none", "summary", "full"}
        assert DEFAULT_DISPLAY == "summary"
        for mode in DISPLAY_MODES:
            assert validate_display(mode) == mode

    @pytest.mark.parametrize("bad", ["everything", "", None, 1, "FULL"])
    def test_rejects_invalid_values(self, bad) -> None:
        with pytest.raises(ValueError, match="display"):
            validate_display(bad)


class TestRenderLastExpression:
    def test_none_mode_renders_nothing(self) -> None:
        assert render_last_expression({"a": 1}, "none") is None

    def test_summary_keeps_small_repr(self) -> None:
        assert render_last_expression(42, "summary") == "42"
        assert render_last_expression([1, 2], "summary") == "[1, 2]"

    def test_summary_bounds_large_values(self) -> None:
        value = "x" * (SUMMARY_PREVIEW_BYTES * 4)
        rendered = render_last_expression(value, "summary")
        assert rendered is not None
        assert rendered.startswith("[summary str")
        assert "len=2048" in rendered
        assert "truncated" in rendered
        assert len(rendered.encode("utf-8")) < len(value.encode("utf-8"))

    def test_full_mode_returns_complete_repr(self) -> None:
        value = "y" * (SUMMARY_PREVIEW_BYTES * 4)
        rendered = render_last_expression(value, "full")
        assert rendered == repr(value)

    def test_unrepresentable_value_degrades(self) -> None:
        class Hostile:
            def __repr__(self) -> str:
                raise RuntimeError("nope")

        rendered = render_last_expression(Hostile(), "summary")
        assert "unrepresentable" in rendered

    def test_invalid_display_raises(self) -> None:
        with pytest.raises(ValueError):
            render_last_expression(1, "verbose")
