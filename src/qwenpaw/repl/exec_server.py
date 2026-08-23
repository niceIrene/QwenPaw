# -*- coding: utf-8 -*-
"""Persistent CodeAct exec server (design doc §2.3; roadmap §2.3-§2.9).

One sandboxed Python process hosts a persistent user namespace.  Cells run
on worker threads so the stdio loop can keep servicing nested tool calls
(and honor ``interrupt``/``restore`` messages) while a cell is in flight.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from .errors import classify_exception, make_error
from .output_policy import (
    DEFAULT_DISPLAY,
    DEFAULT_STDOUT_LIMIT,
    bound_output,
    bound_traceback,
    validate_display,
)
from .protocol import (
    MAX_KERNEL_MESSAGE_BYTES,
    MAX_TOOL_RESULT_BYTES,
    ProtocolError,
    read_message,
    write_message,
)
from .proxy_runtime import PawToolError, build_namespace, new_tool_call_id


class CellLintError(SyntaxError):
    """Raised when model-authored code contains a forbidden P0 pattern."""


def _is_silent_handler(node: ast.ExceptHandler) -> bool:
    catches_broad = node.type is None or (
        isinstance(node.type, ast.Name) and node.type.id == "Exception"
    )
    if not catches_broad or not node.body:
        return False
    return all(
        isinstance(item, ast.Pass)
        or (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and item.value.value is Ellipsis
        )
        for item in node.body
    )


def lint_tree(tree: ast.AST) -> None:
    """Reject silent broad exception handlers and direct shell execution."""

    relaxed = bool(os.getenv("QWENPAW_REPL_RELAXED"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _is_silent_handler(node):
            raise CellLintError(
                "silent `except:` / `except Exception:` handlers are "
                "forbidden; surface or handle the error explicitly",
            )
        if relaxed or not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and (
                (function.value.id == "os" and function.attr == "system")
                or function.value.id == "subprocess"
            )
        ):
            raise CellLintError(
                "shell/subprocess execution is unavailable in repl_exec; "
                "use pathlib/os for filesystem traversal, or a structured "
                "shell tool only when direct tools are permitted",
            )


class _CapturedFDs:
    """Capture stdout/stderr at the file-descriptor boundary when possible."""

    def __init__(self) -> None:
        self._temporary: Any = None
        self._saved: tuple[int, int] | None = None
        self._fallback = io.StringIO()
        self._redirect_out: Any = None
        self._redirect_err: Any = None

    def __enter__(self) -> "_CapturedFDs":
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            stdout_fd = sys.stdout.fileno()
            stderr_fd = sys.stderr.fileno()
            self._temporary = tempfile.TemporaryFile(mode="w+b")
            self._saved = (os.dup(stdout_fd), os.dup(stderr_fd))
            os.dup2(self._temporary.fileno(), stdout_fd)
            os.dup2(self._temporary.fileno(), stderr_fd)
        except (AttributeError, io.UnsupportedOperation, OSError):
            self._saved = None
            self._redirect_out = contextlib.redirect_stdout(self._fallback)
            self._redirect_err = contextlib.redirect_stderr(self._fallback)
            self._redirect_out.__enter__()
            self._redirect_err.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._saved is None:
            if self._redirect_err is not None:
                self._redirect_err.__exit__(exc_type, exc, tb)
            if self._redirect_out is not None:
                self._redirect_out.__exit__(exc_type, exc, tb)
            return
        sys.stdout.flush()
        sys.stderr.flush()
        stdout_fd = sys.stdout.fileno()
        stderr_fd = sys.stderr.fileno()
        os.dup2(self._saved[0], stdout_fd)
        os.dup2(self._saved[1], stderr_fd)
        os.close(self._saved[0])
        os.close(self._saved[1])

    @property
    def text(self) -> str:
        if self._saved is None:
            return self._fallback.getvalue()
        self._temporary.flush()
        self._temporary.seek(0)
        return self._temporary.read().decode("utf-8", errors="replace")


class StdioKernelChannel:
    """Thread-safe nested protocol dispatcher used while a cell is running.

    ``tool_call`` messages are written to the main process; the matching
    ``tool_result`` replies are routed back to the waiting cell thread(s)
    through per-call queues fed by the serve loop.  Multiple cell threads
    may have tool calls in flight at once (roadmap P2).
    """

    def __init__(self, protocol_out: TextIO) -> None:
        self._protocol_out = protocol_out
        self._write_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._update_tools = lambda _tools: None
        self.shutdown_requested = False
        self.tool_trace: list[dict[str, str]] = []

    def set_tool_updater(self, updater: Any) -> None:
        self._update_tools = updater

    def send(self, message: Mapping[str, Any]) -> None:
        with self._write_lock:
            write_message(
                self._protocol_out,
                message,
                max_bytes=MAX_KERNEL_MESSAGE_BYTES,
            )

    def deliver(self, message: Mapping[str, Any]) -> bool:
        """Route one inbound ``tool_result`` to its waiting call thread."""
        call_id = str(message.get("id") or "")
        with self._pending_lock:
            inbox = self._pending.get(call_id)
        if inbox is None:
            return False
        inbox.put(dict(message))
        return True

    def abort_all(self) -> None:
        """Wake every waiting call thread (shutdown/kernel death)."""
        with self._pending_lock:
            inboxes = list(self._pending.values())
        for inbox in inboxes:
            inbox.put(None)

    def call_tool(self, tool: str, args: dict[str, Any]) -> Any:
        call_id = new_tool_call_id()
        inbox: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[call_id] = inbox
        try:
            self.send(
                {
                    "id": call_id,
                    "type": "tool_call",
                    "tool": tool,
                    "args": args,
                },
            )
            while True:
                message = inbox.get()
                if message is None:
                    raise KeyboardInterrupt("REPL shutdown requested")
                if bool(message.get("ok")):
                    self.tool_trace.append({"tool": tool, "status": "ok"})
                    return message.get("value")
                error = message.get("error")
                error_data = error if isinstance(error, Mapping) else {}
                kind = str(error_data.get("kind") or "failed")
                text = str(error_data.get("message") or "tool call failed")
                suggestion = str(error_data.get("suggestion") or "")
                if suggestion:
                    text = f"{text} {suggestion}"
                self.tool_trace.append({"tool": tool, "status": kind})
                raise PawToolError(kind, tool, text)
        finally:
            with self._pending_lock:
                self._pending.pop(call_id, None)


def _visible_variables(
    namespace: Mapping[str, Any],
) -> dict[str, tuple[Any, ...]]:
    internal = set(namespace.get("_paw_internal_names") or ())
    result: dict[str, tuple[Any, ...]] = {}
    for name, value in namespace.items():
        if name in internal or name.startswith("__"):
            continue
        try:
            fingerprint = repr(value)[:1000]
        except Exception:  # pragma: no cover - hostile user object
            fingerprint = f"<unreprable:{id(value)}>"
        result[name] = (type(value).__name__, id(value), fingerprint)
    return result


def _vars_delta(
    before: Mapping[str, tuple[Any, ...]],
    namespace: Mapping[str, Any],
) -> list[dict[str, Any]]:
    after = _visible_variables(namespace)
    delta: list[dict[str, Any]] = []
    for name, fingerprint in after.items():
        if before.get(name) == fingerprint:
            continue
        value = namespace[name]
        try:
            size = len(value)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            try:
                size = sys.getsizeof(value)
            except TypeError:
                size = 0
        delta.append(
            {"name": name, "type": type(value).__name__, "size": size},
        )
    return delta


def _execute_tree(
    tree: ast.Module,
    namespace: dict[str, Any],
    display: str = DEFAULT_DISPLAY,
) -> None:
    """Execute a cell, rendering the final expression per the display mode."""
    from .output_policy import render_last_expression

    if (
        display == "none"
        or not tree.body
        or not isinstance(tree.body[-1], ast.Expr)
    ):
        exec(compile(tree, "<cell>", "exec"), namespace)  # noqa: S102
        return
    prefix = ast.Module(body=tree.body[:-1], type_ignores=tree.type_ignores)
    if prefix.body:
        exec(compile(prefix, "<cell>", "exec"), namespace)  # noqa: S102
    expression = ast.Expression(tree.body[-1].value)
    value = eval(
        compile(expression, "<cell>", "eval"),
        namespace,
    )  # noqa: S307
    if value is not None:
        rendered = render_last_expression(value, display)
        if rendered is not None:
            print(rendered)


def execute_cell_code(
    code: str,
    namespace: dict[str, Any],
    display: str = DEFAULT_DISPLAY,
) -> None:
    """Parse, lint, and execute one cell (shared by all backends)."""
    if not isinstance(code, str):
        raise TypeError("exec.code must be a string")
    tree = ast.parse(code, filename="<cell>", mode="exec")
    lint_tree(tree)
    _execute_tree(tree, namespace, validate_display(display))


def _error_only_result(
    exec_id: str,
    error: dict[str, Any],
    *,
    traceback_text: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": exec_id,
        "type": "exec_result",
        "ok": False,
        "stdout": "",
        "spilled": [],
        "vars_delta": [],
        "tool_trace": [],
        "error": error,
    }
    if traceback_text:
        result["traceback"] = bound_traceback(traceback_text)
    return result


def _run_cell(
    message: Mapping[str, Any],
    namespace: dict[str, Any],
    channel: StdioKernelChannel,
    workspace: Path,
    stdout_limit: int,
    backend: Any = None,
) -> dict[str, Any]:
    exec_id = str(message["id"])
    code = message.get("code")
    try:
        display = validate_display(message.get("display", DEFAULT_DISPLAY))
    except ValueError as exc:
        return _error_only_result(
            exec_id,
            make_error(
                "validation_error",
                code="invalid_display",
                message=str(exc),
            ),
        )
    before = _visible_variables(namespace)
    channel.tool_trace = []
    captured = _CapturedFDs()
    error_text = ""
    ok = False
    error: dict[str, Any] | None = None
    try:
        if backend is None:
            from .backend import CustomExecBackend

            backend = CustomExecBackend(namespace, workspace)
        with captured:
            backend.execute(code, display)
        ok = True
    except BaseException as exc:  # noqa: BLE001 - traceback is the result
        error_text = traceback.format_exc()
        error = classify_exception(exc)

    bounded = bound_output(
        captured.text,
        workspace=workspace,
        spill_name=f"spill_{exec_id}.txt",
        limit=stdout_limit,
    )
    result: dict[str, Any] = {
        "id": exec_id,
        "type": "exec_result",
        "ok": ok,
        "stdout": bounded.text,
        "spilled": list(bounded.spilled),
        "vars_delta": _vars_delta(before, namespace),
        "tool_trace": list(channel.tool_trace),
    }
    if not ok:
        result["traceback"] = bound_traceback(error_text)
        result["error"] = error or make_error(
            "runtime_error",
            message="cell failed without an exception",
        )
        result["interrupted"] = result["error"]["kind"] == "interrupted"
    return result


class _ActiveCell:
    """State for the cell currently executing on a worker thread."""

    def __init__(self, exec_id: str) -> None:
        self.exec_id = exec_id
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.interrupt_injected = False


# pylint: disable-next=too-many-branches,too-many-statements
def _serve_message_loop(
    inbox: queue.Queue,
    channel: StdioKernelChannel,
    backend: Any,
    namespace: dict[str, Any],
    workspace: Path,
    stdout_limit: int,
    session_tag: str,
    update_tools: Any,
) -> None:
    active: _ActiveCell | None = None
    while True:
        if active is not None and active.done.is_set():
            _finalize_cell(active, channel, backend, session_tag)
            if channel.shutdown_requested:
                break
            active = None
            continue
        try:
            message = inbox.get(timeout=0.1 if active is not None else None)
        except queue.Empty:
            continue
        if message is None:
            if active is None:
                break
            # stdin closed while a cell is still running: drain the cell so
            # its exec_result is not silently dropped.
            while not active.done.wait(timeout=0.1):
                continue
            _finalize_cell(active, channel, backend, session_tag)
            break
        message_type = message["type"]
        if message_type == "shutdown":
            channel.shutdown_requested = True
            if active is not None:
                active.interrupt_injected = backend.interrupt()
                channel.abort_all()
            else:
                break
            continue
        if message_type == "tool_list_update":
            tools = message.get("tools")
            if isinstance(tools, list):
                update_tools(tools)
            continue
        if message_type == "tool_result":
            if not channel.deliver(message):
                channel.send(
                    {
                        "id": "-",
                        "type": "log",
                        "level": "error",
                        "message": (
                            "ignored tool_result for unknown call: "
                            f"{message.get('id')}"
                        ),
                    },
                )
            continue
        if message_type == "interrupt":
            if (
                active is not None
                and str(message.get("id")) == active.exec_id
                and not active.interrupt_injected
            ):
                # The worker thread may not have registered yet; retry
                # briefly so a timeout interrupt is never silently lost.
                for _attempt in range(50):
                    if backend.interrupt():
                        active.interrupt_injected = True
                        break
                    if active.done.is_set():
                        break
                    time.sleep(0.02)
            continue
        if message_type == "restore":
            _handle_restore(message, active, backend, channel)
            continue
        if message_type != "exec":
            channel.send(
                {
                    "id": "-",
                    "type": "log",
                    "level": "error",
                    "message": f"ignored unexpected message: {message_type}",
                },
            )
            continue
        if active is not None:
            channel.send(
                _error_only_result(
                    str(message["id"]),
                    make_error(
                        "failed",
                        code="kernel_busy",
                        message="another cell is already running",
                        retryable=True,
                    ),
                ),
            )
            continue
        active = _start_cell(
            message,
            backend,
            namespace,
            channel,
            workspace,
            stdout_limit,
        )


def _start_cell(
    message: Mapping[str, Any],
    backend: Any,
    namespace: dict[str, Any],
    channel: StdioKernelChannel,
    workspace: Path,
    stdout_limit: int,
) -> _ActiveCell:
    exec_id = str(message["id"])
    active = _ActiveCell(exec_id)

    def worker() -> None:
        current = threading.current_thread()
        backend.register_worker(current)
        try:
            active.result = _run_cell(
                message,
                namespace,
                channel,
                workspace,
                stdout_limit,
                backend=backend,
            )
        except BaseException as exc:  # pragma: no cover - defensive
            active.result = _error_only_result(
                exec_id,
                classify_exception(exc),
                traceback_text=traceback.format_exc(),
            )
        finally:
            backend.clear_worker()
            active.done.set()

    thread = threading.Thread(
        target=worker,
        name=f"repl-cell-{exec_id}",
        daemon=True,
    )
    thread.start()
    return active


def _finalize_cell(
    active: _ActiveCell,
    channel: StdioKernelChannel,
    backend: Any,
    session_tag: str,
) -> None:
    result = active.result or _error_only_result(
        active.exec_id,
        make_error("failed", message="cell ended without a result"),
    )
    if result.get("ok") and not channel.shutdown_requested:
        # Crash-recovery snapshot; best effort and bounded (roadmap §2.7).
        with contextlib.suppress(Exception):
            backend.snapshot(f"{session_tag}/latest")
    try:
        channel.send(result)
    except ProtocolError:
        channel.send(
            _error_only_result(
                active.exec_id,
                make_error(
                    "result_too_large",
                    message=(
                        "exec_result exceeded the protocol limit; outputs "
                        "were spilled to the workspace"
                    ),
                ),
            ),
        )


def _handle_restore(
    message: Mapping[str, Any],
    active: _ActiveCell | None,
    backend: Any,
    channel: StdioKernelChannel,
) -> None:
    response: dict[str, Any] = {
        "id": str(message["id"]),
        "type": "restore_result",
        "ok": False,
        "restored": [],
    }
    if active is not None:
        response["error"] = make_error(
            "failed",
            code="kernel_busy",
            message="cannot restore while a cell is running",
            retryable=True,
        )
        channel.send(response)
        return
    snapshot_id = str(message.get("snapshot_id") or "latest")
    if (
        not snapshot_id
        or ".." in snapshot_id
        or snapshot_id.startswith("/")
        or len(snapshot_id) > 192
    ):
        response["error"] = make_error(
            "validation_error",
            code="invalid_snapshot_id",
            message=f"unsafe snapshot id: {snapshot_id!r}",
        )
        channel.send(response)
        return
    try:
        restored = backend.restore(snapshot_id)
        response["ok"] = True
        response["restored"] = list(restored)
    except FileNotFoundError as exc:
        response["error"] = make_error(
            "failed",
            code="snapshot_missing",
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - restore must never crash
        response["error"] = make_error(
            "failed",
            code=type(exc).__name__,
            message=str(exc),
        )
    channel.send(response)


def serve(
    stdin: TextIO,
    protocol_out: TextIO,
    *,
    workspace: Path,
) -> int:
    """Serve messages until shutdown or EOF."""
    init = read_message(stdin, max_bytes=MAX_TOOL_RESULT_BYTES)
    if init is None:
        return 0
    if init["type"] != "init":
        raise ProtocolError("the first REPL message must be init")
    tools = init.get("tools")
    config = init.get("config")
    tool_list = tools if isinstance(tools, list) else []
    config_data = config if isinstance(config, Mapping) else {}
    stdout_limit = int(
        config_data.get("stdout_limit") or DEFAULT_STDOUT_LIMIT,
    )
    session_tag = str(config_data.get("session_tag") or "default")

    from .backend import make_backend, resolve_backend_kind

    channel = StdioKernelChannel(protocol_out)
    namespace, update_tools = build_namespace(channel, workspace, tool_list)
    channel.set_tool_updater(update_tools)
    backend = make_backend(
        resolve_backend_kind(str(config_data.get("backend") or "")),
        namespace,
        workspace,
    )

    inbox: queue.Queue = queue.Queue()

    def reader() -> None:
        while True:
            try:
                message = read_message(
                    stdin,
                    max_bytes=MAX_TOOL_RESULT_BYTES,
                )
            except ProtocolError as exc:
                channel.send(
                    {
                        "id": "-",
                        "type": "log",
                        "level": "error",
                        "message": f"protocol error: {exc}",
                    },
                )
                inbox.put(None)
                return
            inbox.put(message)
            if message is None:
                return

    reader_thread = threading.Thread(
        target=reader,
        name="repl-reader",
        daemon=True,
    )
    reader_thread.start()

    try:
        _serve_message_loop(
            inbox,
            channel,
            backend,
            namespace,
            workspace,
            stdout_limit,
            session_tag,
            update_tools,
        )
    finally:
        channel.abort_all()
        backend.close()
    return 0


def main() -> int:
    """CLI entry point launched by :class:`KernelManager`."""
    workspace_raw = os.environ.get("QWENPAW_REPL_WORKSPACE", "")
    if not workspace_raw:
        raise RuntimeError("QWENPAW_REPL_WORKSPACE is required")
    workspace = Path(workspace_raw).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    protocol_out = os.fdopen(
        os.dup(sys.stdout.fileno()),
        "w",
        encoding="utf-8",
        buffering=1,
    )
    try:
        return serve(sys.stdin, protocol_out, workspace=workspace)
    finally:
        protocol_out.close()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CellLintError",
    "StdioKernelChannel",
    "execute_cell_code",
    "lint_tree",
    "main",
    "serve",
]
