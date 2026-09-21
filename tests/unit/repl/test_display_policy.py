# -*- coding: utf-8 -*-
"""Display policy for the final cell expression (roadmap §2.3)."""

from __future__ import annotations

import pytest

from qwenpaw.repl.output_policy import (
    DEFAULT_DISPLAY,
    DISPLAY_MODES,
    SUMMARY_PREVIEW_BYTES,
    render_last_expression,
    validate_display,
    bound_output,
    DEFAULT_STDOUT_LIMIT,
    MAX_STDOUT_LIMIT,
    MIN_STDOUT_LIMIT,
    OVERFLOW_HEAD_BYTES,
    OVERFLOW_MARKER,
    STDOUT_LIMIT_ENV,
    resolve_stdout_limit,
    stdout_limit_for,
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


def test_failed_spill_points_at_the_variables_not_a_rerun(tmp_path) -> None:
    """On a read-only workspace the overflow cannot be saved. The data is
    normally still in a variable, so the notice must send the model there — a
    "re-run" hint makes it repeat the retrieval it already did."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "out").write_text("a file where the spill dir should go")

    bounded = bound_output(
        "x" * (DEFAULT_STDOUT_LIMIT * 2),
        workspace=workspace,
        spill_name="spill_test.txt",
    )

    assert bounded.spilled == ()
    assert bounded.text.startswith(OVERFLOW_MARKER)
    assert "could not be spilled (workspace read-only)" in bounded.text
    assert "print LESS" in bounded.text
    assert "instead of re-running the call" in bounded.text
    assert "Re-run with a smaller output" not in bounded.text
    assert len(bounded.text.encode("utf-8")) <= DEFAULT_STDOUT_LIMIT


def test_overflow_shows_a_short_head_not_a_full_window(tmp_path) -> None:
    lines = "".join(f"row {i:05d} {'y' * 40}\n" for i in range(2000))

    bounded = bound_output(
        lines,
        workspace=tmp_path,
        spill_name="spill_head.txt",
        limit=DEFAULT_STDOUT_LIMIT,
    )

    assert bounded.spilled == ("out/spill_head.txt",)
    notice, _, head = bounded.text.partition("]\n")
    assert f"over the {DEFAULT_STDOUT_LIMIT}-byte limit" in notice
    assert "original REPL variables" in notice
    assert "do not print the whole spill" in notice
    # A head, not the first 8 KB: the model should re-print, not read on.
    assert 0 < len(head.encode("utf-8")) <= OVERFLOW_HEAD_BYTES
    assert head.startswith("row 00000")
    assert head.endswith("\n")  # cut at a line boundary
    assert (tmp_path / "out/spill_head.txt").read_text() == lines


def test_output_under_the_limit_is_untouched(tmp_path) -> None:
    bounded = bound_output("small", workspace=tmp_path, spill_name="s.txt")
    assert bounded.text == "small"
    assert bounded.spilled == ()


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (None, DEFAULT_STDOUT_LIMIT),  # unknown: today's behaviour
        (0, DEFAULT_STDOUT_LIMIT),
        (4_000, MIN_STDOUT_LIMIT),  # tiny window: floor
        (32_768, 8_192),  # a 32k model gets exactly the old 8 KB
        (65_536, 16_384),
        (131_072, MAX_STDOUT_LIMIT),  # 128k and up: ceiling
        (1_000_000, MAX_STDOUT_LIMIT),
    ],
)
def test_stdout_limit_scales_with_the_context_window(window, expected):
    assert stdout_limit_for(window) == expected


def test_stdout_limit_resolution_order(monkeypatch) -> None:
    monkeypatch.delenv(STDOUT_LIMIT_ENV, raising=False)
    assert resolve_stdout_limit(None, 1_000_000) == MAX_STDOUT_LIMIT
    assert resolve_stdout_limit(512, 1_000_000) == 512  # explicit pins it
    monkeypatch.setenv(STDOUT_LIMIT_ENV, "16384")
    assert resolve_stdout_limit(512, 1_000_000) == 16384  # operator wins
    monkeypatch.setenv(STDOUT_LIMIT_ENV, "not-a-number")
    assert resolve_stdout_limit(None, 65_536) == 16_384

