"""Daemon helpers: start/status/log (roadmap P2)."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from qwenpaw.repl.daemon import (
    MAX_LOG_TAIL_BYTES,
    daemon_log,
    daemon_status,
    start_daemon,
    validate_daemon_name,
)


class TestValidateDaemonName:
    def test_accepts_safe_names(self) -> None:
        for name in ("server", "worker-1", "a.b_c", "X" * 64):
            assert validate_daemon_name(name) == name

    @pytest.mark.parametrize("bad", ["", "a b", "a/b", "..", "x" * 65, None])
    def test_rejects_unsafe_names(self, bad) -> None:
        with pytest.raises(ValueError):
            validate_daemon_name(bad)


@pytest.fixture
def daemon_factory():
    started: list[int] = []

    def _start(workspace: Path, name: str, code: str) -> dict:
        info = start_daemon(
            [sys.executable, "-c", code],
            name,
            workspace,
        )
        started.append(info["pid"])
        return info

    yield _start

    for pid in started:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_start_status_and_log(tmp_path: Path, daemon_factory) -> None:
    info = daemon_factory(
        tmp_path,
        "worker",
        "import sys; print('booted', flush=True); sys.stderr.write('warn\\n')",
    )
    assert info["name"] == "worker"
    assert info["pid"] > 0
    assert info["log"] == ".qwenpaw-repl/daemons/worker.log"

    deadline = time.monotonic() + 5
    status = daemon_status("worker", tmp_path)
    while "booted" not in status["log_tail"] and time.monotonic() < deadline:
        time.sleep(0.05)
        status = daemon_status("worker", tmp_path)

    assert status["pid"] == info["pid"]
    assert "booted" in status["log_tail"]
    # stderr is folded into the same log.
    full_log = daemon_log("worker", tmp_path)
    assert "booted" in full_log
    assert "warn" in full_log


def test_daemon_log_is_bounded(tmp_path: Path, daemon_factory) -> None:
    daemon_factory(tmp_path, "chatty", "print('x' * 5000, flush=True)")
    deadline = time.monotonic() + 5
    tail = daemon_log("chatty", tmp_path, max_bytes=100)
    while not tail and time.monotonic() < deadline:
        time.sleep(0.05)
        tail = daemon_log("chatty", tmp_path, max_bytes=100)
    assert 0 < len(tail) <= 100
    with pytest.raises(ValueError):
        daemon_log("chatty", tmp_path, max_bytes=MAX_LOG_TAIL_BYTES + 1)


def test_status_for_missing_daemon(tmp_path: Path) -> None:
    status = daemon_status("ghost", tmp_path)
    assert status == {
        "name": "ghost",
        "pid": None,
        "alive": False,
        "log_tail": "",
    }


def test_start_rejects_bad_commands(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        start_daemon([], "empty", tmp_path)
    with pytest.raises(ValueError):
        start_daemon(123, "bad-type", tmp_path)
    with pytest.raises(ValueError):
        start_daemon("ls", "../bad-name", tmp_path)
