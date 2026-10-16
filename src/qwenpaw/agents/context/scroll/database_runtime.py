# -*- coding: utf-8 -*-
"""SQL scroll history with disposable SQLite-compatible recall views."""

import asyncio
from dataclasses import replace
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory

from ....sandbox.config import MountSpec
from ....storage.backends.sqlite.recall_snapshot import (
    refresh_history_snapshot,
)
from ....storage.errors import StorageError
from ....utils.io_utils import run_sync_io
from .. import ScrollComponents, scroll_unsandboxed_allowed
from .manager import ScrollContextManager
from .memoryspace import DEFAULT_ROW_CAP
from .recall_tool import RecallLoopGuard, make_recall_history
from .repl import make_recall_history_python


class DatabaseScrollManager(ScrollContextManager):
    """Own a history handle and serialize recall with snapshot teardown."""

    def __init__(self, *, temporary, **kwargs):
        super().__init__(**kwargs)
        self.temporary = temporary
        self.snapshot_path = Path(temporary.name) / "history.db"
        self.recall_lock = asyncio.Lock()
        self.recall_closed = False

    async def prepare_recall(self, op: str, arguments: dict) -> None:
        """Export only the records a structured operation can actually read."""
        selection = {}
        if op == "expand":
            lo, hi = arguments.get("lo"), arguments.get("hi")
            if lo is None or hi is None:
                return
            selection = {
                "after": lo - 1,
                "through": hi,
                "session_id": self._session_id,
                "agent_id": self._agent_id,
                "include_unassigned_agent": True,
                # The extra row preserves MemorySpace's truncation sentinel.
                "limit": DEFAULT_ROW_CAP + 1,
            }
        elif op == "recall_tool":
            tool_id = arguments.get("tool_call_id")
            if not (tool_id or "").strip():
                return
            selection = {
                "tool_call_id": tool_id,
                "agent_id": self._agent_id,
                "limit": DEFAULT_ROW_CAP + 1,
            }
        elif op != "search":
            return
        await refresh_history_snapshot(
            self._history,
            self.snapshot_path,
            search_index=op == "search",
            **selection,
        )

    def wrap_recall(self, tool, *, full_snapshot: bool = True):
        """Serialize recall, with a full snapshot only for the REPL."""

        @wraps(tool)
        async def recall(*args, **kwargs):
            async with self.recall_lock:
                if self.recall_closed:
                    raise StorageError("Recall history is closed")
                if full_snapshot:
                    await refresh_history_snapshot(
                        self._history,
                        self.snapshot_path,
                    )
                sandbox = kwargs.get("sandbox_config")
                if sandbox is not None:
                    kwargs["sandbox_config"] = replace(
                        sandbox,
                        mounts=[
                            *sandbox.mounts,
                            MountSpec(
                                path=self.temporary.name,
                                executable=False,
                            ),
                        ],
                    )
                return await tool(*args, **kwargs)

        recall._tool_descriptor = replace(  # pylint: disable=protected-access
            tool._tool_descriptor,  # pylint: disable=protected-access
            func=recall,  # pylint: disable=protected-access
        )
        return recall

    async def aclose(self) -> None:
        async with self.recall_lock:
            self.recall_closed = True
            try:
                await super().aclose()
            finally:
                await run_sync_io(self.temporary.cleanup)


async def build_database_scroll_components(
    *,
    session,
    agent_config,
    workspace_dir,
    session_id,
    offloader=None,
) -> ScrollComponents | None:
    """Bind the configured SQL runtime; never fall back to a local store."""
    lcc = agent_config.running.light_context_config
    if lcc.strategy != "scroll":
        return None
    if session.runtime is None:
        raise StorageError("Session storage is not open")
    history = await session.runtime.history(session.scope)
    temporary = None
    try:
        temporary = await run_sync_io(
            TemporaryDirectory,
            prefix="qwenpaw-recall-",
        )
        sc = lcc.scroll_config
        guard = RecallLoopGuard()
        manager = DatabaseScrollManager(
            temporary=temporary,
            history=history,
            session_id=session_id,
            agent_id=session.scope.agent_id,
            offloader=offloader if sc.offload_dialog else None,
            recall_loop_guard=guard,
        )
        recall = make_recall_history(
            history_db_path=str(manager.snapshot_path),
            session_id=session_id,
            agent_id=session.scope.agent_id,
            loop_guard=guard,
            prepare_read=manager.prepare_recall,
            page_max_bytes=(
                lcc.tool_result_pruning_config.pruning_recent_msg_max_bytes
            ),
        )
        repl = make_recall_history_python(
            history_db_path=str(manager.snapshot_path),
            session_id=session_id,
            agent_id=session.scope.agent_id,
            scratch_root=str(Path(workspace_dir) / ".scroll"),
            timeout_s=sc.repl_timeout_s,
            allow_unsandboxed=scroll_unsandboxed_allowed(sc),
        )
        return ScrollComponents(
            manager,
            manager.wrap_recall(repl),
            manager.wrap_recall(recall, full_snapshot=False),
        )
    except BaseException:
        await history.close()
        if temporary is not None:
            await run_sync_io(temporary.cleanup)
        raise
