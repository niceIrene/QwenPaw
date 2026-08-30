# -*- coding: utf-8 -*-
"""Detached daemon helpers for the CodeAct kernel (roadmap P2).

``paw.daemon(command, name)`` spawns a detached subprocess whose stdout and
stderr are appended to ``workspace/.qwenpaw-repl/daemons/<name>.log``.
Daemons only survive across cells when the sandbox was launched without a
PID namespace (``QWENPAW_REPL_NO_PIDNS`` under the relaxed benchmark
profile); otherwise the strict sandbox reaps them with the cell, which is
the intended fail-closed behavior.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

DAEMONS_SUBDIR = Path(".qwenpaw-repl") / "daemons"
DEFAULT_LOG_TAIL_BYTES = 500
MAX_LOG_TAIL_BYTES = 4096

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def validate_daemon_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name) or ".." in name:
        raise ValueError(
            "daemon name must match [A-Za-z0-9_.-]{1,64} without '..': "
            f"{name!r}",
        )
    return name


def daemons_dir(workspace: Path) -> Path:
    directory = Path(workspace).resolve() / DAEMONS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _log_path(workspace: Path, name: str) -> Path:
    return daemons_dir(workspace) / f"{name}.log"


def _pid_path(workspace: Path, name: str) -> Path:
    return daemons_dir(workspace) / f"{name}.pid"


def start_daemon(
    command: Any,
    name: Any,
    workspace: Path,
) -> dict[str, Any]:
    """Launch one detached process and record its pid/log locations."""
    validated = validate_daemon_name(name)
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, (list, tuple)):
        argv = [str(item) for item in command]
    else:
        raise ValueError("daemon command must be a string or list of strings")
    if not argv:
        raise ValueError("daemon command must not be empty")

    log_path = _log_path(workspace, validated)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        # The child intentionally outlives this function and is tracked by PID.
        # pylint: disable-next=consider-using-with
        process = subprocess.Popen(  # noqa: S603 - model-owned argv
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(Path(workspace).resolve()),
        )
    _pid_path(workspace, validated).write_text(
        str(process.pid),
        encoding="utf-8",
    )
    return {
        "name": validated,
        "pid": process.pid,
        "log": (DAEMONS_SUBDIR / f"{validated}.log").as_posix(),
    }


def _read_pid(workspace: Path, name: str) -> int | None:
    try:
        raw = _pid_path(workspace, name).read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    # Daemons are never waitpid()ed, so exited children linger as zombies and
    # os.kill(pid, 0) would report them alive forever. Reap our own child
    # first: a returned pid means the process has exited.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _tail(path: Path, max_bytes: int) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            sample = handle.read(max_bytes)
    except OSError:
        return ""
    return sample.decode("utf-8", errors="replace")


def daemon_status(
    name: Any,
    workspace: Path,
    *,
    tail_bytes: int = DEFAULT_LOG_TAIL_BYTES,
) -> dict[str, Any]:
    """Return pid/liveness/log tail for one named daemon."""
    validated = validate_daemon_name(name)
    if not 1 <= int(tail_bytes) <= MAX_LOG_TAIL_BYTES:
        raise ValueError(
            f"tail_bytes must be between 1 and {MAX_LOG_TAIL_BYTES}",
        )
    pid = _read_pid(workspace, validated)
    return {
        "name": validated,
        "pid": pid,
        "alive": _process_alive(pid),
        "log_tail": _tail(_log_path(workspace, validated), int(tail_bytes)),
    }


def daemon_log(
    name: Any,
    workspace: Path,
    max_bytes: int = 2000,
) -> str:
    """Return the bounded tail of one daemon's log file."""
    validated = validate_daemon_name(name)
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_LOG_TAIL_BYTES
    ):
        raise ValueError(
            f"max_bytes must be an integer between 1 and {MAX_LOG_TAIL_BYTES}",
        )
    return _tail(_log_path(workspace, validated), max_bytes)


__all__ = [
    "DEFAULT_LOG_TAIL_BYTES",
    "MAX_LOG_TAIL_BYTES",
    "daemon_log",
    "daemon_status",
    "daemons_dir",
    "start_daemon",
    "validate_daemon_name",
]
