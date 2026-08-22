# -*- coding: utf-8 -*-
"""Tests for runtime prompt contributors."""

from types import SimpleNamespace

import pytest

from qwenpaw.runtime.prompt_contributors import (
    CodingModeContributor,
    CodeActIdentityContributor,
    ScrollContextContributor,
    WorkspacePromptFilesContributor,
    build_default_prompt_manager,
)


def _ctx(tmp_path, system_prompt_files):
    return SimpleNamespace(
        workspace_dir=str(tmp_path),
        agent_id="test_agent",
        extras={
            "agent_config": SimpleNamespace(
                system_prompt_files=system_prompt_files,
                language="en",
            ),
            "heartbeat_enabled": False,
            "language": "en",
            "memory_manager": None,
        },
    )


def _harness_ctx(
    tmp_path,
    system_prompt_files,
    *,
    enabled=True,
    text=None,
    tool_routing="hybrid",
):
    return SimpleNamespace(
        workspace_dir=str(tmp_path),
        agent_id="test_agent",
        extras={
            "agent_config": SimpleNamespace(
                system_prompt_files=system_prompt_files,
                language="en",
                codeact=SimpleNamespace(
                    enabled=enabled,
                    identity_text=text,
                    tool_routing=tool_routing,
                ),
                running=SimpleNamespace(
                    light_context_config=SimpleNamespace(strategy="scroll"),
                ),
            ),
            "heartbeat_enabled": False,
            "language": "en",
            "memory_manager": None,
        },
    )


def test_harness_identity_absent_when_disabled(tmp_path):
    assert (
        CodeActIdentityContributor().contribute_sync(
            _harness_ctx(tmp_path, [], enabled=False),
        )
        is None
    )
    # No codeact config at all must also stay silent.
    assert (
        CodeActIdentityContributor().contribute_sync(
            _ctx(tmp_path, []),
        )
        is None
    )


def test_harness_identity_emits_default_text_when_enabled(tmp_path):
    fragment = CodeActIdentityContributor().contribute_sync(
        _harness_ctx(tmp_path, []),
    )
    assert fragment is not None
    body = str(fragment)
    assert "autonomous task-execution agent" in body
    assert "# Role" in body
    assert "pick the most reasonable interpretation" in body
    # Revised text drops the contested lines.
    assert "reversible" not in body
    assert "ignore persona" not in body


def test_harness_identity_honours_override_text(tmp_path):
    fragment = CodeActIdentityContributor().contribute_sync(
        _harness_ctx(tmp_path, [], text="custom harness identity"),
    )
    assert fragment == "custom harness identity"


def test_workspace_prompt_files_suppressed_under_harness(tmp_path):
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _harness_ctx(tmp_path, ["SOUL.md", "PROFILE.md"]),
    )

    assert fragment is None


def test_scroll_context_still_active_under_harness(tmp_path):
    """Harness runs use the scroll strategy, so its guidance stays on."""
    fragment = ScrollContextContributor().contribute_sync(
        _harness_ctx(tmp_path, []),
    )
    assert fragment is not None
    assert "durably recorded" in fragment


def test_scroll_context_repl_only_variant_for_codeact_repl_only(tmp_path):
    """repl-only routing hides the structured recall_history teaching."""
    fragment = ScrollContextContributor().contribute_sync(
        _harness_ctx(tmp_path, [], tool_routing="repl-only"),
    )

    assert fragment is not None
    assert "recall_history_python" in fragment
    assert "ms.search" in fragment
    assert 'recall_history(op="search"' not in fragment


def test_scroll_context_standard_variant_for_codeact_hybrid(tmp_path):
    """hybrid routing keeps the structured recall_history teaching."""
    fragment = ScrollContextContributor().contribute_sync(
        _harness_ctx(tmp_path, [], tool_routing="hybrid"),
    )

    assert fragment is not None
    assert 'recall_history(op="search"' in fragment
    assert "recall_history_python" not in fragment


def test_default_prompt_manager_under_harness_mode(tmp_path):
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")

    prompt = build_default_prompt_manager().build_sync(
        _harness_ctx(tmp_path, ["SOUL.md", "PROFILE.md"]),
    )

    assert "autonomous task-execution agent" in prompt
    assert "soul body" not in prompt
    assert "profile body" not in prompt
    # The multi-agent identity header stays (harmless, pinned elsewhere).
    assert "# Agent Identity" in prompt


def test_default_prompt_manager_unchanged_without_harness(tmp_path):
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")

    prompt = build_default_prompt_manager().build_sync(
        _ctx(tmp_path, ["SOUL.md"]),
    )

    assert "soul body" in prompt
    assert "autonomous task-execution agent" not in prompt


def test_workspace_prompt_files_respects_disabled_files(tmp_path):
    """Configured prompt file list gates SOUL.md and PROFILE.md."""
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")

    prompt = build_default_prompt_manager().build_sync(
        _ctx(tmp_path, ["AGENTS.md"]),
    )

    assert "# AGENTS.md" in prompt
    assert "agents body" in prompt
    assert "# SOUL.md" not in prompt
    assert "soul body" not in prompt
    assert "# PROFILE.md" not in prompt
    assert "profile body" not in prompt


def test_workspace_prompt_files_empty_list_disables_workspace_markdown(
    tmp_path,
):
    """An empty configured list is meaningful and disables markdown files."""
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("soul body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, []),
    )

    assert fragment is None


def test_workspace_prompt_files_preserves_configured_order_and_custom_files(
    tmp_path,
):
    """Configured file order is the rendered prompt order."""
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")
    (tmp_path / "PROFILE.md").write_text("profile body", encoding="utf-8")
    (tmp_path / "CUSTOM.md").write_text("custom body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, ["PROFILE.md", "CUSTOM.md", "AGENTS.md"]),
    )

    assert fragment is not None
    assert fragment.index("# PROFILE.md") < fragment.index("# CUSTOM.md")
    assert fragment.index("# CUSTOM.md") < fragment.index("# AGENTS.md")


def test_workspace_prompt_files_skips_parent_traversal(tmp_path):
    """Configured prompt files cannot escape the workspace via ``..``."""
    outside = tmp_path.parent / f"{tmp_path.name}_secret.md"
    outside.write_text("secret body", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents body", encoding="utf-8")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, [f"../{outside.name}", "AGENTS.md"]),
    )

    assert fragment is not None
    body = str(fragment)
    assert "secret body" not in body
    assert "agents body" in body


def test_workspace_prompt_files_skips_symlink_escape(tmp_path):
    """Configured prompt files cannot escape through symlinks."""
    outside = tmp_path.parent / f"{tmp_path.name}_symlink_secret.md"
    outside.write_text("secret body", encoding="utf-8")
    link = tmp_path / "LINK.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    fragment = WorkspacePromptFilesContributor().contribute_sync(
        _ctx(tmp_path, ["LINK.md"]),
    )

    assert fragment is None


def test_coding_mode_does_not_treat_generic_memory_as_internal_access(
    tmp_path,
):
    """Task memory must not be confused with QwenPaw's private workspace."""
    internal_workspace = tmp_path / "qwenpaw"
    project = tmp_path / "task"
    ctx = SimpleNamespace(
        workspace_dir=str(internal_workspace),
        extras={
            "agent_config": SimpleNamespace(
                coding_mode=SimpleNamespace(enabled=True),
                project_dir=str(project),
            ),
        },
    )

    fragment = CodingModeContributor().contribute_sync(ctx)

    assert fragment is not None
    assert str(project) in fragment
    assert str(internal_workspace) in fragment
    assert "Generic references to" in fragment
    assert '"memory"' in fragment
    assert "do NOT grant access" in fragment
