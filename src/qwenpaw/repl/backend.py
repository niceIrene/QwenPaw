# -*- coding: utf-8 -*-
"""ExecutionBackend abstraction for the CodeAct kernel (roadmap §2.7).

``CustomExecBackend`` is the current lightweight ast/exec engine.
``IPythonBackend`` optionally runs cells through IPython inside the same
strict sandbox for top-level ``await`` and richer tracebacks; it falls back
to the custom engine whenever IPython is unavailable or misbehaves.

Selection happens via ``QWENPAW_REPL_BACKEND=custom|ipython`` (default
``custom``), forwarded by the manager in the ``init`` config.  All tool
calls still leave the kernel through the GovernanceBridge regardless of
backend.
"""

from __future__ import annotations

import abc
import ast
import ctypes
import os
import threading
from pathlib import Path
from typing import Any

from . import persistence

BACKEND_CUSTOM = "custom"
BACKEND_IPYTHON = "ipython"
VALID_BACKENDS = frozenset({BACKEND_CUSTOM, BACKEND_IPYTHON})

# Snapshot identity used for automatic crash recovery.
LATEST_SNAPSHOT_ID = "latest"


def resolve_backend_kind(requested: str | None = None) -> str:
    """Resolve the backend kind from config/env with fail-safe defaults."""
    raw = requested or os.getenv("QWENPAW_REPL_BACKEND", "") or BACKEND_CUSTOM
    kind = str(raw).strip().lower()
    return kind if kind in VALID_BACKENDS else BACKEND_CUSTOM


class ExecutionBackend(abc.ABC):
    """One in-kernel cell execution engine bound to the user namespace."""

    name: str = "abstract"

    def __init__(self, namespace: dict[str, Any], workspace: Path) -> None:
        self.namespace = namespace
        self.workspace = Path(workspace)
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------------ exec
    @abc.abstractmethod
    def _run(self, code: str, display: str) -> None:
        """Execute one cell; raise on failure."""

    def execute(self, code: str, display: str = "summary") -> None:
        """Execute one cell on the current worker thread."""
        self._run(code, display)

    # ------------------------------------------------------------- interrupt
    def register_worker(self, thread: threading.Thread) -> None:
        with self._worker_lock:
            self._worker_thread = thread

    def clear_worker(self) -> None:
        with self._worker_lock:
            self._worker_thread = None

    def interrupt(self) -> bool:
        """Inject KeyboardInterrupt into the cell worker thread.

        Returns ``True`` when the asynchronous exception was queued. The
        caller grants a grace period before the manager hard-kills the
        kernel (roadmap §2.8).
        """
        with self._worker_lock:
            thread = self._worker_thread
        if thread is None or not thread.is_alive():
            return False
        thread_id = thread.ident
        if thread_id is None:
            return False
        # PyThreadState_SetAsyncExc takes an unsigned long thread id.
        count = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread_id),
            ctypes.py_object(KeyboardInterrupt),
        )
        if count == 0:
            return False
        if count > 1:  # pragma: no cover - "if it returns a number greater
            # than one, you're in trouble" per the CPython docstring.
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id),
                None,
            )
            return False
        return True

    # ------------------------------------------------------- state handling
    def _snapshot_root(self) -> Path:
        return self.workspace / persistence.SNAPSHOTS_SUBDIR

    def snapshot(self, snapshot_id: str = LATEST_SNAPSHOT_ID) -> list[str]:
        """Persist the user namespace for later crash recovery."""
        destination = self._snapshot_root() / snapshot_id
        return persistence.snapshot_namespace(self.namespace, destination)

    def restore(self, snapshot_id: str = LATEST_SNAPSHOT_ID) -> list[str]:
        """Restore a previous snapshot into the live namespace."""
        source = self._snapshot_root() / snapshot_id
        return persistence.restore_namespace(self.namespace, source)

    def close(self) -> None:
        """Release backend resources (idempotent)."""
        self.clear_worker()


class CustomExecBackend(ExecutionBackend):
    """The current lightweight parse/lint/exec engine."""

    name = BACKEND_CUSTOM

    def _run(self, code: str, display: str) -> None:
        from .exec_server import execute_cell_code

        execute_cell_code(code, self.namespace, display)


class IPythonBackend(ExecutionBackend):
    """IPython-powered engine (top-level await, richer tracebacks).

    Any setup or per-cell failure degrades to the custom engine so a broken
    IPython install can never take the REPL down.
    """

    name = BACKEND_IPYTHON

    def __init__(self, namespace: dict[str, Any], workspace: Path) -> None:
        super().__init__(namespace, workspace)
        from IPython.core.interactiveshell import InteractiveShell

        self._shell = InteractiveShell.instance(user_ns=namespace)
        self._shell.ast_transformers = []
        self._fallback = CustomExecBackend(namespace, workspace)

    def _run(self, code: str, display: str) -> None:
        from .exec_server import lint_tree

        # Lint with the same rules every backend must honor.
        lint_tree(ast.parse(code, filename="<cell>", mode="exec"))
        if display == "none":
            original = self._shell.displayhook
            try:
                self._shell.displayhook = lambda *_args, **_kwargs: None
                result = self._shell.run_cell(code, store_history=False)
            finally:
                self._shell.displayhook = original
        else:
            result = self._shell.run_cell(code, store_history=False)
        error_in_exec = getattr(result, "error_in_exec", None)
        if error_in_exec is not None:
            raise error_in_exec
        success = getattr(result, "success", True)
        if not success:
            raise RuntimeError("IPython cell execution failed")


def make_backend(
    kind: str,
    namespace: dict[str, Any],
    workspace: Path,
) -> ExecutionBackend:
    """Create the requested backend, falling back to custom on any failure."""
    resolved = resolve_backend_kind(kind)
    if resolved == BACKEND_IPYTHON:
        try:
            return IPythonBackend(namespace, workspace)
        except Exception:  # noqa: BLE001 - fail-safe engine degradation
            pass
    return CustomExecBackend(namespace, workspace)


__all__ = [
    "BACKEND_CUSTOM",
    "BACKEND_IPYTHON",
    "CustomExecBackend",
    "ExecutionBackend",
    "IPythonBackend",
    "LATEST_SNAPSHOT_ID",
    "VALID_BACKENDS",
    "make_backend",
    "resolve_backend_kind",
]
