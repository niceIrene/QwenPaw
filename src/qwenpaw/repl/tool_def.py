# -*- coding: utf-8 -*-
"""Coding Mode ``repl_exec`` tool definition (design doc §2.7)."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_registry import (
    ToolDescriptor,
    ToolGovernanceSpec,
    ToolUISpec,
)
from .errors import make_error, render_error_json
from .governance_bridge import GovernanceBridge, ToolForwardingError
from .kernel_manager import (
    KernelCrashedError,
    KernelUnavailableError,
    get_default_kernel_manager,
)
from .output_policy import validate_display

_logger = logging.getLogger(__name__)

REPL_DESCRIPTION = """Execute Python in a persistent, sandboxed CodeAct REPL.

Use repl_exec for multi-step data processing, filtering or aggregating large
tool results, and work that benefits from variables surviving across calls.
Unlike execute_python, this kernel is stateful. Inside code, call registered
tools with paw.tools.<name>(...) or MCP tools with
paw.tools.mcp.<server>.<tool>(...); every call still follows normal QwenPaw
policy and approval. Explore progressively with dir(paw.tools) and help(...).
The built-in help(tool) prints documentation and returns None; call it as a
standalone statement, or use inspect.signature(tool) when code needs a value.

Do not use repl_exec for a single tool action unless the current task requires
REPL-only routing. Treat the kernel as the data plane and the model context as
a small control plane. (The behavioural rules — assign results to variables
and reuse them, never print large results, verify before finishing — live in
the CodeAct system prompt; the points below are the tool's mechanics.)
- The namespace persists across cells: imports, function/class definitions,
  and data loaded into variables all survive. After you import a module or
  read a file/dataset into a variable once, reference it directly in later
  cells; do not re-import modules or re-read the same file every cell.
- These names are already bound in every cell — do not import or redefine
  them: the modules json, re, pathlib, collections (plus pandas/pd when
  installed), and the helpers paw, workspace, workspace_path. Import only
  what is not on this list (for example numpy).
- Batch homogeneous tool calls in Python loops and transform/filter/sort data
  in the kernel.
- Inspect large values with print() on bounded projections — type/len/shape,
  head slices, .head() for DataFrames — never print a whole large value.
- The sandbox has no general /tmp or internal QwenPaw workspace access. Use
  workspace_path(relpath) to resolve files under the workspace and normal
  pathlib reads/writes for bounded previews and durable state. Spill files
  are archival; keep transforming the original variables instead of printing
  a whole spill.
- Shell and subprocess execution are unavailable. Use pathlib/os for file
  traversal, or a structured shell tool when direct tools are permitted.
- Complete required writes and external mutations before optional validation;
  do not postpone the task's primary side effects to a final recovery cell.
- Silent broad exception handlers are rejected before execution. Never use
  ``except: pass`` or ``except Exception: pass``; handle a specific exception,
  record an actionable error, or re-raise it.

Discovery: ``paw.list_tools()``, ``paw.search_tools(query)`` and
``paw.describe_tool(path)`` enumerate the governed tools available inside the
REPL. The tool list is static for the whole session — call
``paw.list_tools()`` at most once, remember its result, and do not repeat
keyword searches for a capability you already located.

Background processes: ``paw.daemon(command, name)`` starts a detached process
for work that must keep running while you do other things;
``paw.daemon_status(name)`` and ``paw.daemon_log(name)`` inspect it. If a
step must wait for a daemon, wait inside ONE cell
(``while paw.daemon_status(name)["alive"]: time.sleep(15)``) instead of
spending one repl_exec call per status check.

Errors are returned as one JSON object with fields kind/code/message/
retryable/suggestion; the ``kind`` field selects recovery (see the CodeAct
system prompt), and budget_exhausted means stop.

Long-running cells: a cell that exceeds its time budget is not killed; it
keeps running in the background and its variables are retained. The next
repl_exec waits for that background cell first; while it is still running
you get a retryable kernel_busy error. On a timeout or kernel_busy, never
resubmit the same code — the work is already in progress. If it should finish
soon, wait and retry. If it is clearly stuck and will not finish, call
repl_exec(reset=True) once to abandon it and restart the kernel: state
restores to the last completed cell, so you lose only the stuck cell. Do not
keep polling a stuck kernel — that just burns your cell budget. For genuinely
long or parallel background processes use paw.daemon(command, name); do not
use multiprocessing or ProcessPoolExecutor inside cells — pool workers
deadlock in this sandbox.
"""


def _workspace_id(workspace: Path) -> str:
    return hashlib.sha256(
        str(workspace.resolve()).encode("utf-8"),
    ).hexdigest()[:16]


def render_observation(result: Any) -> str:
    """Render only bounded cell output and summaries into model context."""
    sections: list[str] = []
    if result.stdout:
        sections.append(result.stdout.rstrip())
    if result.spilled:
        sections.append(
            f"[{len(result.spilled)} large output(s) spilled: "
            f"{', '.join(result.spilled)}]",
        )
    if not result.ok and result.error:
        sections.append(f"[repl error] {render_error_json(result.error)}")
    if not result.ok and result.traceback:
        sections.append(result.traceback.rstrip())
    if result.vars_delta:
        variables = " ".join(
            f"+{item.get('name')}({item.get('type')},{item.get('size')})"
            for item in result.vars_delta
        )
        sections.append(f"vars: {variables}")
    if result.tool_trace:
        trace = " -> ".join(
            f"{item.get('tool')}[{item.get('status')}]"
            for item in result.tool_trace
        )
        sections.append(f"tool_trace: {trace}")
    if result.kernel_restarted:
        sections.append("[repl] kernel restarted; all variables were lost")
    return "\n".join(item for item in sections if item) or "ok"


@dataclass
class ReplRuntimeBinding:
    """Request-scoped dependencies that must survive tool task boundaries."""

    workspace: Path | None
    toolkit: Any = None
    agent_state: Any = None
    soft_cell_limit: int | None = None
    hard_cell_limit: int | None = None
    cell_count: int = 0
    session_id: str = ""


def _positive_int(value: Any) -> int | None:
    """Return a positive integer request option, excluding booleans."""

    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _budget_note(binding: ReplRuntimeBinding) -> str:
    """Render a compact control-plane reminder near the cell budget."""

    hard = binding.hard_cell_limit
    soft = binding.soft_cell_limit
    if hard is not None and binding.cell_count >= hard:
        return (
            f"[repl budget] Cell {binding.cell_count}/{hard} used. "
            "This was the final executable cell. Validate retained outputs "
            "now and answer without another repl_exec call."
        )
    if soft is not None and binding.cell_count >= soft:
        suffix = f"/{hard}" if hard is not None else ""
        return (
            f"[repl budget] Cell {binding.cell_count}{suffix} used. "
            "Stop exploring: use retained variables to finish, write, and "
            "validate the requested outputs."
        )
    return ""


def make_repl_only_toolkit(toolkit: Any) -> Any:
    """Return a model-facing Toolkit exposing ``repl_exec`` + recall REPL.

    The original toolkit remains intact and is bound to the REPL bridge for
    governed nested calls.  ``recall_history_python`` stays top-level: it is
    its own sandboxed recall kernel, not a governed in-cell tool (CodeAct
    mode requires a sandbox, so it is always registered on this path; when
    absent the toolkit degrades to ``repl_exec`` alone).  This makes
    REPL-only routing an execution boundary instead of a prompt convention.
    """

    from agentscope.tool import Toolkit

    top_level = {"repl_exec", "recall_history_python"}
    kept = [
        tool
        for group in getattr(toolkit, "tool_groups", ()) or ()
        for tool in getattr(group, "tools", ()) or ()
        if getattr(tool, "name", None) in top_level
    ]
    repl_tools = [
        tool for tool in kept if getattr(tool, "name", None) == "repl_exec"
    ]
    if len(repl_tools) != 1:
        raise RuntimeError(
            "REPL-only routing requires exactly one repl_exec tool; "
            f"found {len(repl_tools)}",
        )
    if len(kept) == 1:
        _logger.warning(
            "REPL-only routing: recall_history_python not registered "
            "(no sandbox?) — model toolkit exposes only repl_exec",
        )
    return Toolkit(tools=kept)


def _governed_workspace(governor: Any) -> Path | None:
    """Resolve the request's validated Coding Mode project directory."""

    candidate = getattr(governor, "coding_project_dir", None) or getattr(
        governor,
        "workspace_dir",
        None,
    )
    if candidate is None:
        return None
    return Path(candidate).expanduser().resolve()


def bind_repl_exec_runtime(
    toolkit: Any,
    agent_state: Any,
    *,
    forwarding_toolkit: Any = None,
    request_context: dict[str, Any] | None = None,
) -> int:
    """Bind the finished agent runtime to each request-scoped REPL tool."""

    bound = 0
    for group in getattr(toolkit, "tool_groups", ()) or ():
        for tool in getattr(group, "tools", ()) or ():
            if getattr(tool, "name", None) != "repl_exec":
                continue
            func = getattr(tool, "_func", tool)
            binding = getattr(func, "_repl_runtime_binding", None)
            if not isinstance(binding, ReplRuntimeBinding):
                continue
            binding.toolkit = forwarding_toolkit or toolkit
            binding.agent_state = agent_state
            context = request_context or {}
            session_id = context.get("session_id")
            binding.session_id = str(session_id or "")
            binding.soft_cell_limit = _positive_int(
                context.get("codeact_repl_soft_limit"),
            )
            binding.hard_cell_limit = _positive_int(
                context.get("codeact_repl_hard_limit"),
            )
            if (
                binding.soft_cell_limit is not None
                and binding.hard_cell_limit is not None
            ):
                binding.soft_cell_limit = min(
                    binding.soft_cell_limit,
                    binding.hard_cell_limit,
                )
            binding.cell_count = 0
            bound += 1
    return bound


def make_repl_exec_tool(governor: Any) -> Any:
    """Create the request-safe tool closure bound to its ResourceGovernor."""
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolChunk
    from ..config.context import get_current_workspace_dir

    binding = ReplRuntimeBinding(workspace=_governed_workspace(governor))

    async def repl_exec(
        code: str,
        display: str = "summary",
        reset: bool = False,
    ) -> ToolChunk:
        """Execute one cell in the persistent sandboxed CodeAct REPL."""
        if isinstance(reset, str):
            reset = reset.strip().lower() in {"1", "true", "yes", "on"}
        else:
            reset = bool(reset)
        try:
            display = validate_display(display)
        except ValueError as exc:
            return ToolChunk(
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "[repl error] "
                            + render_error_json(
                                make_error(
                                    "validation_error",
                                    code="invalid_display",
                                    message=str(exc),
                                ),
                            )
                        ),
                    ),
                ],
                metadata={"repl": True},
            )
        hard_limit = binding.hard_cell_limit
        if hard_limit is not None and binding.cell_count >= hard_limit:
            budget_error = make_error(
                "budget_exhausted",
                message=(
                    "repl_exec cell budget exhausted "
                    f"({hard_limit}/{hard_limit}); the cell was not executed"
                ),
            )
            return ToolChunk(
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "[repl error] "
                            + render_error_json(budget_error)
                            + "\nAnswer using retained results."
                        ),
                    ),
                ],
                metadata={
                    "repl": True,
                    "budget_exhausted": True,
                    "error": budget_error,
                    "cell_count": binding.cell_count,
                    "hard_cell_limit": hard_limit,
                },
            )
        binding.cell_count += 1
        # AgentScope may execute tools in a task whose ContextVars were
        # captured before QwenPaw's request hooks ran. The REPL is constructed
        # once per request, so prefer its explicitly bound, governor-validated
        # project and retain ContextVar lookup only for compatibility with
        # direct callers.
        workspace = binding.workspace or get_current_workspace_dir()
        if workspace is None:
            return ToolChunk(
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text="repl_exec requires an active workspace",
                    ),
                ],
            )
        workspace = Path(workspace).resolve()
        workspace_id = _workspace_id(workspace)
        try:
            bridge = await GovernanceBridge.from_current_context(
                workspace=workspace,
                workspace_id=workspace_id,
                toolkit=binding.toolkit,
                agent_state=binding.agent_state,
            )
            manager = get_default_kernel_manager()
            handle = await manager.get_or_start(
                workspace_id=workspace_id,
                workspace=workspace,
                specs=bridge.specs,
                governor=governor,
                session_id=binding.session_id,
            )
            if reset:
                handle = await manager.reset_kernel(
                    handle,
                    governor=governor,
                    specs=bridge.specs,
                )
            result = await manager.execute(
                handle,
                code,
                bridge,
                display=display,
            )
            state = (
                ToolResultState.SUCCESS if result.ok else ToolResultState.ERROR
            )
            observation = render_observation(result)
            budget_note = _budget_note(binding)
            if budget_note:
                observation = f"{observation}\n{budget_note}"
            return ToolChunk(
                state=state,
                content=[
                    TextBlock(
                        type="text",
                        text=observation,
                    ),
                ],
                metadata={
                    "repl": True,
                    "cell_count": binding.cell_count,
                    "soft_cell_limit": binding.soft_cell_limit,
                    "hard_cell_limit": binding.hard_cell_limit,
                    "spilled": list(result.spilled),
                    "vars_delta": list(result.vars_delta),
                    "tool_trace": list(result.tool_trace),
                },
            )
        except (
            KernelUnavailableError,
            KernelCrashedError,
            ToolForwardingError,
            TypeError,
            ValueError,
        ) as exc:
            return ToolChunk(
                state=ToolResultState.ERROR,
                content=[
                    TextBlock(
                        type="text",
                        text=f"{type(exc).__name__}: {exc}",
                    ),
                ],
            )

    repl_exec.__name__ = "repl_exec"
    repl_exec.__qualname__ = "repl_exec"
    repl_exec.__doc__ = REPL_DESCRIPTION
    setattr(repl_exec, "_repl_runtime_binding", binding)
    setattr(
        repl_exec,
        "_tool_descriptor",
        ToolDescriptor(
            name="repl_exec",
            func=repl_exec,
            enabled_by_default=False,
            requires_modes=("coding",),
            async_execution=True,
            description=REPL_DESCRIPTION.splitlines()[0],
            metadata={"codeact_repl": True},
            governance=ToolGovernanceSpec(
                tool_type="internal",
                policy_name="ReplExec",
            ),
            ui=ToolUISpec(
                description="Persistent sandboxed Python with governed tools",
                icon="terminal",
                display_to_user=True,
            ),
        ),
    )
    return repl_exec


__all__ = [
    "REPL_DESCRIPTION",
    "ReplRuntimeBinding",
    "bind_repl_exec_runtime",
    "make_repl_only_toolkit",
    "make_repl_exec_tool",
    "render_observation",
]
