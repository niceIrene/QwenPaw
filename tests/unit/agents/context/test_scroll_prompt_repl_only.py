# -*- coding: utf-8 -*-
"""Tests for the repl-only variant of the scroll system prompt.

CodeAct repl-only mode does not expose the structured ``recall_history``
tool top-level, so the scroll prompt swaps its recall paragraph for
``recall_history_python`` / ``ms`` wording. These tests pin both variants
and guarantee the swap touches only the recall block.
"""

from qwenpaw.agents.context.scroll.prompt import (
    SCROLL_SYSTEM_PROMPT,
    SCROLL_SYSTEM_PROMPT_ZH,
    build_scroll_system_prompt,
)


def test_default_prompt_is_byte_identical_to_template():
    assert build_scroll_system_prompt("en") == SCROLL_SYSTEM_PROMPT
    assert build_scroll_system_prompt("zh") == SCROLL_SYSTEM_PROMPT_ZH


def test_repl_only_en_teaches_ms_surface():
    prompt = build_scroll_system_prompt("en", repl_only=True)

    assert "recall_history_python" in prompt
    assert 'ms.search("keywords", k=10)' in prompt
    assert "ms.expand(lo, hi)" in prompt
    assert "ms.sql_query" in prompt
    assert "ms.days_between(d1, d2)" in prompt
    assert "all_agents=True" in prompt
    # ms becomes available in the shared repl_exec kernel after first recall.
    assert "repl_exec" in prompt
    # The structured-tool teaching must be gone.
    assert 'recall_history(op="search"' not in prompt
    assert "``recall_history``" not in prompt


def test_repl_only_zh_teaches_ms_surface():
    prompt = build_scroll_system_prompt("zh", repl_only=True)

    assert "recall_history_python" in prompt
    assert "ms.search" in prompt
    assert "ms.expand(lo, hi)" in prompt
    assert "ms.sql_query" in prompt
    assert "all_agents=True" in prompt
    assert 'recall_history(op="search"' not in prompt


def test_repl_only_unknown_language_falls_back_to_english():
    prompt = build_scroll_system_prompt("fr", repl_only=True)

    assert "RETRIEVAL HEADLINE" in prompt
    assert "recall_history_python" in prompt


def test_repl_only_differs_only_in_recall_block():
    standard = build_scroll_system_prompt("en")
    repl = build_scroll_system_prompt("en", repl_only=True)

    assert standard != repl
    # Headline / map / discipline teaching is identical in both variants.
    for marker in ("RETRIEVAL HEADLINE", "THE MAP", "DISCIPLINE"):
        assert marker in standard
        assert marker in repl
    # Everything before the swapped block is untouched.
    assert standard.split("THE MAP")[0] == repl.split("THE MAP")[0]


def test_repl_only_teaches_the_recall_loop():
    """repl-only mode is where the model writes recall code, so the prompt
    must teach the method, not only name the ``ms`` calls."""
    for language, needles in {
        "en": (
            "RECALL LOOP",
            "Variables\n    persist across cells",
            "do not re-run the query",
            "no\n    quoted phrases, parentheses or ``*``",
            "no timestamp",
            "Avoid\n    ``LIKE '%word%'`` scans",
        ),
        "zh": ("RECALL 循环", "不要重新执行查询", "没有时间戳"),
    }.items():
        prompt = build_scroll_system_prompt(language, repl_only=True)
        for needle in needles:
            assert needle in prompt, (language, needle)
        # One ranked, role-filtered, match-centred query over the FTS index.
        assert "conversation_history_fts MATCH ?" in prompt
        assert "snippet(conversation_history_fts" in prompt
        assert "bm25(conversation_history_fts)" in prompt


def test_recall_loop_is_repl_only():
    # The structured tool has no cells, variables or SQL surface.
    for language in ("en", "zh"):
        assert "conversation_history_fts" not in build_scroll_system_prompt(
            language,
        )

