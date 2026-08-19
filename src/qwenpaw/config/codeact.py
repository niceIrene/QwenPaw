# -*- coding: utf-8 -*-
"""Apply CodeAct task-execution mode to an agent.

Used by benchmark / experiment runners (e.g. Terminal-Bench, LOCA) to turn a
qwenpaw agent into a clean general agent: enables the neutral task-execution
identity, switches off the personal-assistant stack (workspace
SOUL/PROFILE/AGENTS files, memory manager + dream cron, heartbeat), and runs
the scroll context strategy. ``tool_routing`` selects how tools are exposed:
'repl-only' (only repl_exec top-level, minimal persona), 'hybrid' (repl_exec
plus direct tools, full persona), or 'off' (identity only; react baseline,
no REPL tool). Additive only; a no-op product-wise when not called.
"""

from __future__ import annotations


def apply_codeact_mode(
    agent_id: str,
    *,
    tool_routing: str = "repl-only",
    identity_text: str | None = None,
    project_dir: str | None = None,
    scroll: bool = True,
) -> None:
    """Persist the CodeAct mode profile for ``agent_id``.

    Non-'off' routing forces ``coding_mode.enabled=True`` (that is what
    registers repl_exec and the coding persona); the product UI will show
    Coding Mode on for such agents, which is accepted for benchmark agents.

    ``scroll=False`` keeps the native context strategy: no recall tools are
    registered and no scroll prompt/map/headliner is injected (ablation arm
    without the long-memory stack).
    """
    from .config import (
        CodeActConfig,
        CodingModeConfig,
        load_agent_config,
        save_agent_config,
    )

    if tool_routing not in ("repl-only", "hybrid", "off"):
        raise ValueError(f"unknown CodeAct tool_routing: {tool_routing!r}")

    config = load_agent_config(agent_id)
    config.codeact = CodeActConfig(
        enabled=True,
        tool_routing=tool_routing,  # type: ignore[arg-type]
        identity_text=identity_text,
    )
    if tool_routing == "off":
        # React baseline: identity only, no REPL tool, no coding persona.
        config.coding_mode = CodingModeConfig(enabled=False)
    else:
        config.coding_mode = CodingModeConfig(
            enabled=True,
            project_dir=project_dir,
            persona="minimal" if tool_routing == "repl-only" else "full",
        )
    # Belt and braces: the CodeAct identity contributor also suppresses
    # these files.
    config.system_prompt_files = []
    # Prompt stack (scroll/recall guidance, bootstrap, memory, skills) picks
    # its language from this field, which defaults to 'zh'; CodeAct runs are
    # English-only benchmarks.
    config.language = "en"
    # Noop memory backend; also stops auto-memory middleware side effects.
    config.running.memory_manager_backend = "none"
    # CodeAct runs default to the scroll context strategy (headline/recall);
    # ablation arms can opt out to the native strategy (no recall tools, no
    # scroll prompt).
    config.running.light_context_config.strategy = (
        "scroll" if scroll else "native"
    )
    # dream_cron_enabled defaults to True and would schedule a nightly job.
    config.running.reme_light_memory_config.dream_cron_enabled = False
    # heartbeat is Optional; None already means the heartbeat is off.
    if config.heartbeat is not None:
        config.heartbeat.enabled = False
    save_agent_config(agent_id, config)


__all__ = ["apply_codeact_mode"]
