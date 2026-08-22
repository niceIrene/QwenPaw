# -*- coding: utf-8 -*-
"""Coding Mode mixin and tool collection helpers.

Provides:

- ``CodingModeMixin`` — mixin class that adds Coding Mode features
  to a ReActAgent (system prompt injection, LSP / AST tools).
- ``collect_coding_tools()`` — standalone function used by
  :class:`AgentBuilder` to collect coding tools without a mixin instance.
- ``_CODING_SYSTEM_PROMPT_TEMPLATE`` — the system prompt template
  referenced by :class:`CodingModeContributor`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...constant import WORKING_DIR
from ...agents.tools import ast_tool
from ...agents.tools._lsp_servers import detect_available_lsp_languages
from ...agents.tools.lsp_tool import make_lsp_tool

logger = logging.getLogger(__name__)


_CODING_SYSTEM_PROMPT_TEMPLATE = """\
## Coding Mode

You are currently operating in **Coding Mode**.

### Task tracking

Before starting any non-trivial task:

1. Pick a short uppercase snake_case slug (≤ 24 chars) that summarises
   the user's intent — e.g. `BLOG`, `BUGFIX_LOGIN`, `REFACTOR_PAYMENT`.
   Fall back to `CODING` if nothing better fits.

2. Create or overwrite `{{SLUG}}_TODO.md` in the active project root
   with your plan:

       # {{SLUG}} — <one-line goal>
       - [ ] step 1
       - [ ] step 2
       ...

3. After each step is *fully done*, immediately flip its `- [ ]` to
   `- [x]` and append the next step if the scope changed.  Never batch
   completions.

4. When you create a `*_TODO.md` that did not exist before, also
   append `*_TODO.md` to `.gitignore` (only if the line is not already
   there).  These files are session-local notes and must not be
   committed.

### Code references

When referencing code in your replies, always use the form
`path/to/file.py:42` (or `:42-58` for line ranges).  This lets the IDE
side jump directly to the location.  Use **relative** paths from the
active project root, not absolute paths.

### Tool preference for code understanding

For "where is X defined / what calls Y / what are the symbols in Z"
questions, prefer the `lsp` tool over `grep_search`.  The `lsp` tool's
own description lists the languages currently available in this
workspace — if your target file's language is not listed there, fall
back to `grep_search`.

For structural pattern queries (e.g. "all functions that take a
`Request` and return `Response`"), prefer the `ast_search` tool.
`ast_search` is **read-only** — when you want to apply a rewrite, read
the matches first, then call `edit_file` for each location.

Fall back to `grep_search` only when LSP / AST cannot answer.

### File operations

File-IO tools (`read_file`, `write_file`, `edit_file`,
`list_directory`, etc.) resolve **relative** paths against the
**agent workspace**, NOT the active project.  Always pass
**absolute paths** rooted at the active project directory (shown
below) — never a bare filename or a path relative to the project.

### Shell commands

`execute_shell_command` defaults its cwd to the **agent workspace**.
When the command should run inside the project, always pass
`cwd="<active project dir>"` (or prefix with
`cd <active project dir> && ...`).  Do NOT assume `ls`, `cat`,
`find`, `git`, etc. land in the project — without an explicit `cwd`
they land in the workspace.

### Working guidelines
1. **Read before you write** — always read the relevant file(s) first.
2. **Prefer targeted edits** — use `edit_file` over full-file \
rewrites whenever possible.
3. **Touch only what you must** — change only what the task requires; \
do not refactor adjacent code or fix unrelated style outside the \
requested scope.
4. **Summarise after each batch** — briefly note what was done and \
what remains.

Keep reasoning concise.  Prefer small, verifiable steps over large \
monolithic changes.

### Active project

The active project directory for this session is:

    {project_dir}

This is **THE** project — do NOT enumerate the agent workspace or
its `coding_projects/` subfolder looking for "which project to work
on".  Sibling directories are unrelated repositories and are out of
scope unless the user explicitly switches.

Every `read_file` / `write_file` / `edit_file` / `list_directory`
call must use an absolute path that starts with the directory above.
Every `execute_shell_command` call that touches project files must
pass `cwd` equal to the directory above.

### Agent workspace

The internal QwenPaw workspace (configs, sessions, memory) is at:

    {workspace_dir}

Do NOT read or write here unless the user explicitly names QwenPaw's internal
workspace or asks to inspect QwenPaw's own state. Generic references to
"memory", "workspace", files, or task data do NOT grant access to this
directory; resolve them against the active project instead.
"""

_CODING_SYSTEM_PROMPT_TEMPLATE_MINIMAL = """\
## Coding Mode

You are operating in **Coding Mode** on a single project. In this session
there are no separate file or shell tools: perform every file read, file
write, and command through Python inside the `repl_exec` session (for
example with `pathlib` and `os`).

### Tools

You call two tools directly. `repl_exec` is your main session — all work
happens there. `recall_history_python` runs Python queries against your own
evicted history through the pre-defined `ms` object (its query surface is
described in its tool documentation); call it directly whenever you need
something that is no longer in context. Both tools execute cells in the SAME
shared persistent kernel: one namespace, cells from both tools run strictly
in order, and variables persist across both — so do not clobber each other's
names, never redefine the reserved `ms` object, and if a recall call returns
`kernel_busy` because a long cell is still running, wait and retry rather
than resubmitting. Recall cells are pure Python plus `ms`: `paw.tools` calls
are blocked inside them. Every other capability is a governed tool reached
from *inside* a `repl_exec` cell as `paw.tools.<name>(...)`, never as a
top-level tool call; whenever an instruction refers to such a tool by bare
name, invoke it as `paw.tools.<name>(...)`. Use `dir(paw.tools)` or
`paw.list_tools()` to see what is available.

### Long-running commands

For a command whose result the next step needs — package installs, builds, a
test run — just run it inline. The single kernel runs cells strictly in order,
so a later cell never starts until this one returns; and if the command outruns
the cell's time budget it keeps running in the background while the next cell
waits (simply retry on a `kernel_busy` error). You do not have to orchestrate
anything for sequential work.

Use `paw.daemon("<shell command>", name="<label>")` only for a process
that must keep running *while you do other work* — a server you need up,
or a genuinely independent parallel job. A daemon returns immediately and
runs outside the kernel; it is not a completion gate. When you later need
its outcome, read `paw.daemon_status(name)` (which reports `alive` and a
`log_tail`) and check the log to confirm it did what you needed before
depending on it. If a step must wait for the daemon to finish, wait inside
ONE cell — `while paw.daemon_status(name)["alive"]: time.sleep(15)` —
instead of spending a separate model turn on each status check.

### Active project

The active project directory for this session is:

    {project_dir}

This is **THE** project — the task's deliverables live here. Read, write, and
run everything against paths under this directory. Do not go looking for
"which project to work on"; sibling directories are unrelated and out of
scope unless the user explicitly switches.

### Agent workspace

QwenPaw's internal workspace (configs, sessions, memory) is at:

    {workspace_dir}

Do NOT read or write here unless the user explicitly names QwenPaw's internal
workspace. Generic references to "memory", "workspace", files, or task data do
NOT grant access to this directory; resolve them against the active project
above instead.

### Working guidelines

1. Read the relevant file(s) before you change them.
2. Change only what the task requires; do not refactor or restyle unrelated
   code outside the requested scope.
3. Keep reasoning concise and prefer small, verifiable steps.
"""


def _collect_codeact_repl_tool(
    governor: object | None,
    request_context: dict[str, str] | None,
) -> list:
    """Return the REPL only when its strict OS sandbox is usable."""
    from ...repl import repl_sandbox_available

    available, reason = repl_sandbox_available(governor, preflight=True)
    if not available:
        logger.info(
            "CodeAct repl_exec not registered (fail-closed): %s",
            reason,
        )
        return []
    from ...governance import PolicyGuardedTool
    from ...repl.tool_def import make_repl_exec_tool

    logger.info("CodeAct repl_exec registered: %s", reason)
    return [
        PolicyGuardedTool(
            make_repl_exec_tool(governor),
            governor=governor,
            request_context=request_context,
        ),
    ]


def _project_dir_from_config(agent_config: object | None) -> str | None:
    """Extract the mode-independent project dir from an agent config."""
    if agent_config is None:
        return None
    if isinstance(agent_config, dict):
        return agent_config.get("project_dir") or None
    return getattr(agent_config, "project_dir", None) or None


class CodingModeMixin:
    """Mixin that adds Coding Mode features to a ReActAgent.

    At runtime this class is mixed into ``QwenPawAgent`` and combined
    with ``Agent`` via MRO. Coding Mode prompt injection is handled by
    :class:`~qwenpaw.runtime.prompt_contributors.CodingModeContributor`.
    """

    def _get_coding_project_dir(self) -> str | None:
        """Return the active coding project dir.

        Request-scoped config wins when present. Otherwise, reload from disk so
        API changes persisted to ``agent.json`` are reflected.

        Returns None when no project has been set (use workspace default).
        """
        from ...config.config import load_agent_config

        agent_config = getattr(self, "_agent_config", None)
        agent_id: str | None = None
        if agent_config is not None:
            if isinstance(agent_config, dict):
                agent_id = agent_config.get("id")
            else:
                agent_id = getattr(agent_config, "id", None)
        if not agent_id:
            agent_id = getattr(self, "name", None)
        if not agent_id:
            return None

        project_dir = _project_dir_from_config(agent_config)
        if project_dir:
            return project_dir

        try:
            config = load_agent_config(agent_id)
            if config.project_dir:
                return config.project_dir
        except Exception:
            logger.debug(
                "Failed to reload agent config for Coding Mode project",
                exc_info=True,
            )

        return None

    def _coding_mode_enabled(self) -> bool:
        """Return ``True`` when Coding Mode is active."""
        agent_config = getattr(self, "_agent_config", None)
        if agent_config is None:
            return False
        if isinstance(agent_config, dict):
            cm = agent_config.get("coding_mode") or {}
            return bool(cm.get("enabled", False))
        cm = getattr(agent_config, "coding_mode", None)
        if cm is None:
            return False
        return bool(getattr(cm, "enabled", False))

    # ------------------------------------------------------------------
    # Tool registration hook (called from QwenPawAgent._create_toolkit)
    # ------------------------------------------------------------------

    def _collect_coding_mode_tools(
        self,
        agent_id: str | None = None,  # pylint: disable=unused-argument
        request_context: dict[str, str] | None = None,
    ) -> list:
        """Collect Coding Mode tool instances (`lsp`, `ast_search`)."""
        if not self._coding_mode_enabled():
            return []

        from ...governance import PolicyGuardedTool

        governor = getattr(self, "_governor", None)
        project_dir = Path(
            self._get_coding_project_dir()
            or str(getattr(self, "_workspace_dir", "") or WORKING_DIR),
        )
        result: list = []
        result.extend(
            _collect_codeact_repl_tool(governor, request_context),
        )

        try:
            available = detect_available_lsp_languages(project_dir)
            if available:
                result.append(
                    PolicyGuardedTool(
                        make_lsp_tool(available),
                        governor=governor,
                        request_context=request_context,
                    ),
                )
                logger.info(
                    "Registered Coding Mode lsp tool with languages: %s",
                    sorted(available.keys()),
                )
            else:
                logger.info(
                    "No LSP servers discovered for project_dir=%s; "
                    "skipping lsp tool",
                    project_dir,
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Failed to register lsp tool: {exc}")

        try:
            if ast_tool.is_ast_grep_available():
                result.append(
                    PolicyGuardedTool(
                        ast_tool.ast_search,
                        governor=governor,
                        request_context=request_context,
                    ),
                )
                logger.info("Registered Coding Mode ast_search tool")
            else:
                logger.info(
                    "ast-grep CLI not found; skipping ast_search tool",
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Failed to register ast_search tool: {exc}")

        return result


def collect_coding_tools(
    agent_config: object,
    workspace_dir: object,
    agent_id: str | None = None,  # pylint: disable=unused-argument
    request_context: dict[str, str] | None = None,
    governor: object | None = None,
) -> list:
    """Collect Coding Mode tools without requiring a mixin instance.

    Standalone replacement for the ``CodingModeMixin.__new__()`` hack
    that ``AgentBuilder`` previously used.
    """
    from ...governance import PolicyGuardedTool

    cm = getattr(agent_config, "coding_mode", None)
    if cm is None or not getattr(cm, "enabled", False):
        return []

    project_dir = Path(
        getattr(agent_config, "project_dir", None)
        or str(workspace_dir or WORKING_DIR),
    )
    result: list = []
    result.extend(
        _collect_codeact_repl_tool(governor, request_context),
    )

    try:
        available = detect_available_lsp_languages(project_dir)
        if available:
            result.append(
                PolicyGuardedTool(
                    make_lsp_tool(available),
                    governor=governor,
                    request_context=request_context,
                ),
            )
            logger.info(
                "Registered Coding Mode lsp tool with languages: %s",
                sorted(available.keys()),
            )
    except Exception as exc:
        logger.warning("Failed to register lsp tool: %s", exc)

    try:
        if ast_tool.is_ast_grep_available():
            result.append(
                PolicyGuardedTool(
                    ast_tool.ast_search,
                    governor=governor,
                    request_context=request_context,
                ),
            )
            logger.info("Registered Coding Mode ast_search tool")
    except Exception as exc:
        logger.warning("Failed to register ast_search tool: %s", exc)

    return result


__all__ = [
    "CodingModeMixin",
    "_CODING_SYSTEM_PROMPT_TEMPLATE",
    "collect_coding_tools",
]
