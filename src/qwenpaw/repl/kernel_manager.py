# -*- coding: utf-8 -*-
"""Sandboxed persistent kernel lifecycle (design doc §2.6, §2.10, §2.11)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..sandbox import MountSpec, SandboxConfig, SandboxMode
from .backend import resolve_backend_kind
from .errors import classify_tool_error, make_error
from .governance_bridge import GovernanceBridge, ToolForwardingError
from .output_policy import DEFAULT_STDOUT_LIMIT
from .persistence import latest_snapshot_dir
from .protocol import (
    MAX_KERNEL_MESSAGE_BYTES,
    ProtocolError,
    decode_message,
    encode_message,
)

logger = logging.getLogger(__name__)

DEFAULT_EXEC_TIMEOUT = 120.0
DEFAULT_IDLE_TIMEOUT = 600.0
#: Grace period given to a soft-interrupted cell before hard-killing (§2.8).
SOFT_INTERRUPT_GRACE = 5.0
#: Maximum in-flight read-only nested tool calls (roadmap P2).
READ_ONLY_CONCURRENCY = 4
#: Telemetry wait for one restore_result message after a kernel restart.
RESTORE_ACK_TIMEOUT = 15.0


class KernelUnavailableError(RuntimeError):
    """The REPL cannot safely start on this platform or configuration."""


class KernelCrashedError(RuntimeError):
    """The child kernel exited before returning an execution result."""


@dataclass(frozen=True)
class ExecResult:
    """Normalized result returned to the ``repl_exec`` tool."""

    ok: bool
    stdout: str = ""
    spilled: tuple[str, ...] = ()
    traceback: str = ""
    vars_delta: tuple[dict[str, Any], ...] = ()
    tool_trace: tuple[dict[str, str], ...] = ()
    kernel_restarted: bool = False
    error: dict[str, Any] | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> "ExecResult":
        """Create a result from one validated ``exec_result`` message."""
        error = message.get("error")
        return cls(
            ok=bool(message.get("ok")),
            stdout=str(message.get("stdout") or ""),
            spilled=tuple(str(item) for item in message.get("spilled") or ()),
            traceback=str(message.get("traceback") or ""),
            vars_delta=tuple(
                dict(item)
                for item in message.get("vars_delta") or ()
                if isinstance(item, dict)
            ),
            tool_trace=tuple(
                dict(item)
                for item in message.get("tool_trace") or ()
                if isinstance(item, dict)
            ),
            error=dict(error) if isinstance(error, dict) else None,
        )


def _session_tag(session_id: str) -> str:
    """Compact stable hash identifying one session's kernel state."""
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:12]


def _kernel_key(workspace_id: str, session_id: str = "") -> str:
    """Kernel identity key: workspace-scoped, or session-scoped (§2.5)."""
    if session_id:
        return f"{workspace_id}::{_session_tag(session_id)}"
    return workspace_id


@dataclass
class KernelHandle:
    """One session/workspace-scoped child process."""

    workspace_id: str
    workspace: Path
    process: asyncio.subprocess.Process
    specs: list[dict[str, Any]]
    specs_hash: str
    session_id: str = ""
    key: str = ""
    cell_index: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    stderr_lines: list[str] = field(default_factory=list)
    stderr_task: asyncio.Task[None] | None = None
    reap_task: asyncio.Task[None] | None = None
    background_task: asyncio.Task[None] | None = None


def repl_sandbox_available(
    governor: Any,
    *,
    preflight: bool = False,
) -> tuple[bool, str]:
    """Return whether P0 can use a strict persistent-process sandbox."""
    if governor is None:
        return False, "resource governor is unavailable"
    globally_enabled = getattr(
        governor,
        "sandbox_globally_enabled",
        getattr(governor, "sandbox_usable", False),
    )
    if not bool(globally_enabled):
        capability = getattr(governor, "sandbox_capability", None)
        reason = getattr(capability, "reason", "sandbox disabled")
        return False, str(reason)
    mode, mode_reason = _repl_sandbox_mode(governor)
    if mode is SandboxMode.NONE:
        return False, mode_reason
    if sys.platform == "win32":
        return False, "P0 CodeAct REPL does not support Windows"
    if preflight:
        return _probe_repl_runtime(mode)
    return True, mode_reason


def _repl_sandbox_mode(governor: Any) -> tuple[SandboxMode, str]:
    """Resolve the strict backend, including container-friendly Bubblewrap."""
    capability = getattr(governor, "sandbox_capability", None)
    mode = getattr(capability, "mode", SandboxMode.NONE)
    if mode in {SandboxMode.SEATBELT, SandboxMode.BUBBLEWRAP}:
        return mode, str(getattr(capability, "reason", mode.value))
    if sys.platform == "linux":
        available, reason = _probe_codeact_bubblewrap()
        if available:
            return SandboxMode.BUBBLEWRAP, reason
    return (
        SandboxMode.NONE,
        f"P0 requires Seatbelt or Bubblewrap, got {mode.value}: "
        f"{getattr(capability, 'reason', 'unavailable')}",
    )


@functools.lru_cache(maxsize=1)
def _probe_codeact_bubblewrap() -> tuple[bool, str]:
    """Probe the Bubblewrap features CodeAct uses inside Docker/Harbor.

    A fresh ``/proc`` mount requires broad Docker capabilities and is not
    necessary for the REPL. The CodeAct profile keeps PID and network
    namespaces but leaves ``/proc`` absent from its empty filesystem.
    """
    executable = shutil.which("bwrap")
    if executable is None:
        return False, "bwrap not found on PATH"
    try:
        result = subprocess.run(  # noqa: S603
            [
                executable,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--unshare-user",
                "--uid",
                "0",
                "--gid",
                "0",
                "--unshare-pid",
                "--unshare-net",
                "--",
                "/bin/true",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"CodeAct bwrap probe failed: {exc}"
    if result.returncode == 0:
        return True, "CodeAct Bubblewrap profile available"
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    return False, f"CodeAct bwrap probe failed: {stderr[:300]}"


def _package_mounts() -> list[MountSpec]:
    import qwenpaw

    paths = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path(sys.executable).absolute(),
        Path(sys.executable).resolve(),
        Path(qwenpaw.__file__).resolve().parent,
    }
    mounts: list[MountSpec] = []
    mounted_paths: list[Path] = []
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        if not path.exists():
            continue
        if any(
            parent == path or parent in path.parents
            for parent in mounted_paths
        ):
            continue
        mounts.append(
            MountSpec(path=str(path), writable=False, executable=True),
        )
        mounted_paths.append(path)
    return mounts


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _repl_sandbox_config(
    governor: Any,
    workspace: Path,
    temporary: Path,
) -> SandboxConfig:
    mode, _reason = _repl_sandbox_mode(governor)
    # Benchmark escape hatch: expose the full disk read-only (workspace stays
    # the only writable path) so cells can drive system binaries such as
    # pdflatex. Deny paths and the network ban still apply.
    relaxed = _env_flag("QWENPAW_REPL_ALLOW_READ_ALL")
    # Further benchmark unlocks, each opt-in and only honored under relaxed:
    # - QWENPAW_REPL_WRITABLE_PATHS: comma-separated extra writable mounts
    #   (system installs/config edits are otherwise impossible from cells).
    # - QWENPAW_REPL_NETWORK: open network (drops the --unshare-net layer).
    # - QWENPAW_REPL_NO_PIDNS: keep spawned daemons alive after the cell
    #   (drops --unshare-pid/--new-session/--die-with-parent).
    network_unlock = relaxed and _env_flag("QWENPAW_REPL_NETWORK")
    no_pidns = relaxed and _env_flag("QWENPAW_REPL_NO_PIDNS")
    mounts = [
        MountSpec(
            path=str(workspace),
            writable=True,
            executable=False,
        ),
        *_package_mounts(),
    ]
    extra_mounts = getattr(governor, "_repl_extra_mounts", None)
    if extra_mounts:
        mounts.extend(extra_mounts)
    if relaxed:
        extra_writable = os.getenv("QWENPAW_REPL_WRITABLE_PATHS")
        if extra_writable is None:
            paths = [
                "/usr/local",
                "/etc",
                "/var",
                "/opt",
                "/root",
                "/home",
            ]
        elif extra_writable.strip().lower() == "none":
            paths = []
        else:
            paths = [
                item.strip()
                for item in extra_writable.split(",")
                if item.strip()
            ]
        workspace_resolved = workspace.resolve()
        for raw in paths:
            candidate = Path(os.path.expanduser(raw))
            if not candidate.exists():
                continue
            resolved = candidate.resolve()
            if (
                resolved == workspace_resolved
                or workspace_resolved in resolved.parents
                or resolved in workspace_resolved.parents
            ):
                continue
            mounts.append(
                MountSpec(path=str(candidate), writable=True, executable=True),
            )
    env_vars = {
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "QWENPAW_WORKING_DIR": str(workspace / ".qwenpaw-runtime"),
        "QWENPAW_REPL_WORKSPACE": str(workspace),
        "TMPDIR": str(temporary),
        "PATH": (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            if relaxed
            else ""
        ),
    }
    if relaxed:
        env_vars["QWENPAW_REPL_RELAXED"] = "1"
    if sys.platform == "linux":
        library_paths = {
            Path(sys.prefix).resolve() / "lib",
            Path(sys.base_prefix).resolve() / "lib",
        }
        existing = sorted(str(path) for path in library_paths if path.is_dir())
        if existing:
            env_vars["LD_LIBRARY_PATH"] = ":".join(existing)
    return SandboxConfig(
        mode=mode,
        workspace_dir=str(workspace),
        mounts=mounts,
        allow_read_all=relaxed,
        deny_paths=[
            "~/.ssh",
            "~/.aws",
            "~/.config/gcloud",
            "~/.kube",
            "~/.gnupg",
        ],
        network_allow=["*"] if network_unlock else [],
        max_processes=32 if relaxed else 1,
        timeout_seconds=int(DEFAULT_EXEC_TIMEOUT),
        env_mode="allowlist",
        env_vars=env_vars,
        platform_hints={
            "strict_workspace_only": not relaxed,
            "repl_network_unlock": network_unlock,
            "repl_no_pidns": no_pidns,
            "python_executables": [
                str(Path(sys.executable).absolute()),
                str(Path(sys.executable).resolve()),
            ],
        },
    )


def _parent_dirs(path: Path) -> list[str]:
    current = path
    parents: list[str] = []
    while str(current) != "/":
        parents.append(str(current))
        current = current.parent
    return list(reversed(parents))


_CLEARENV_SUPPORTED: bool | None = None


def _bwrap_supports_clearenv(executable: str) -> bool:
    """Old bubblewrap builds lack ``--clearenv`` and die on unknown options.

    Skipping it stays safe: the bwrap process itself is spawned with only the
    curated ``config.env_vars`` environment (see ``_launch_argv``), so the
    sandbox never inherits the host environment either way.
    """
    global _CLEARENV_SUPPORTED
    if _CLEARENV_SUPPORTED is None:
        try:
            probe = subprocess.run(
                [executable, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            _CLEARENV_SUPPORTED = "--clearenv" in (probe.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            _CLEARENV_SUPPORTED = True
    return _CLEARENV_SUPPORTED


def _bubblewrap_command(
    config: SandboxConfig,
    argv: list[str],
) -> list[str]:
    from ..sandbox.bubblewrap_sandbox import BubblewrapSandbox

    sandbox = BubblewrapSandbox(config)
    bwrap = sandbox._find_bwrap()  # pylint: disable=protected-access
    hints = config.platform_hints or {}
    network_unlock = bool(hints.get("repl_network_unlock"))
    no_pidns = bool(hints.get("repl_no_pidns"))
    args = [bwrap]
    if not no_pidns:
        # --new-session/--die-with-parent are paired with the PID namespace:
        # without them spawned daemons would still be reaped with bwrap.
        args.extend(["--new-session", "--die-with-parent"])
    args.extend(["--unshare-user", "--uid", "0", "--gid", "0"])
    if not no_pidns:
        args.append("--unshare-pid")
    if not network_unlock:
        args.append("--unshare-net")
    created: set[str] = set()
    if config.allow_read_all:
        # Relaxed benchmark profile: full disk readable, workspace stays the
        # only writable path (layered after the read-only root bind).
        args.extend(["--ro-bind", "/", "/"])
        for mount in config.mounts:
            if not mount.writable or not Path(mount.path).exists():
                continue
            args.extend(["--bind", str(mount.path), str(mount.path)])
        for denied in config.deny_paths or []:
            expanded = Path(os.path.expanduser(denied))
            if expanded.exists():
                args.extend(["--tmpfs", str(expanded)])
    else:
        args.extend(["--tmpfs", "/"])
        for mount in config.mounts:
            source = Path(mount.path).resolve()
            for parent in _parent_dirs(source.parent):
                if parent not in created:
                    args.extend(["--dir", parent])
                    created.add(parent)
            operation = "--bind" if mount.writable else "--ro-bind"
            args.extend([operation, str(source), str(source)])

        # Shared libraries are required to start Python, but no executable
        # directories (/bin, /usr/bin) are exposed.
        for system_path in ("/lib", "/lib64", "/usr/lib"):
            path = Path(system_path)
            if not path.exists():
                continue
            for parent in _parent_dirs(path.parent):
                if parent not in created:
                    args.extend(["--dir", parent])
                    created.add(parent)
            args.extend(["--ro-bind", system_path, system_path])

    # Keep /proc absent. Mounting a fresh procfs requires SYS_ADMIN in the
    # outer Harbor container, while the REPL protocol and Python runtime do
    # not need it.
    args.extend(["--dev", "/dev"])
    # ``--tmpfs /`` starts writable so bwrap can create mount points. Freeze
    # that root after layering the explicit mounts; the workspace remains a
    # separate writable bind mount.
    args.extend(["--remount-ro", "/"])
    args.extend(["--chdir", config.workspace_dir])
    if _bwrap_supports_clearenv(args[0]):
        args.extend(["--clearenv"])
    for key, value in config.env_vars.items():
        args.extend(["--setenv", str(key), str(value)])
    args.extend(["--", *argv])
    return args


def _seatbelt_command(
    config: SandboxConfig,
    argv: list[str],
) -> list[str]:
    from ..sandbox.macos_sandbox import MacOSSandbox

    sandbox = MacOSSandbox(config)
    profile = (
        sandbox._compile_seatbelt_profile()
    )  # pylint: disable=protected-access
    executable = shutil.which("sandbox-exec")
    if executable is None:
        raise KernelUnavailableError(
            "sandbox-exec disappeared after capability probe"
        )
    return [executable, "-p", profile, *argv]


def _specs_hash(specs: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(specs, sort_keys=True, ensure_ascii=False).encode("utf-8"),
    ).hexdigest()


def _stable_tool_specs(
    current: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Update tool metadata without changing the established list prefix.

    Removed tools intentionally remain in the completion cache. Resolution
    still happens authoritatively in the main process, where calling a stale
    proxy produces the normal unavailable-tool guidance.
    """
    incoming_by_path = {
        str(item.get("path")): dict(item)
        for item in incoming
        if item.get("path")
    }
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for old in current:
        path = str(old.get("path") or "")
        if not path or path in seen:
            continue
        merged.append(incoming_by_path.get(path, dict(old)))
        seen.add(path)
    for item in incoming:
        path = str(item.get("path") or "")
        if path and path not in seen:
            merged.append(dict(item))
            seen.add(path)
    return merged


@functools.lru_cache(maxsize=4)
def _probe_repl_runtime(mode: SandboxMode) -> tuple[bool, str]:
    """Verify that the exact sandbox policy can start QwenPaw's Python.

    The platform capability probe only proves that Seatbelt/Bubblewrap exists.
    CodeAct has a narrower filesystem view, so registration additionally
    checks its Python runtime allowlist once per process.
    """
    with tempfile.TemporaryDirectory(prefix="qwenpaw-repl-probe-") as raw:
        workspace = Path(raw).resolve()
        temporary = workspace / "tmp"
        temporary.mkdir()
        governor = type(
            "_ProbeGovernor",
            (),
            {"sandbox_capability": type("_Capability", (), {"mode": mode})()},
        )()
        config = _repl_sandbox_config(governor, workspace, temporary)
        python = str(Path(sys.executable).absolute())
        child_argv = [
            python,
            "-c",
            "import qwenpaw.repl.exec_server",
        ]
        try:
            if mode is SandboxMode.SEATBELT:
                argv = _seatbelt_command(config, child_argv)
            elif mode is SandboxMode.BUBBLEWRAP:
                argv = _bubblewrap_command(config, child_argv)
            else:
                return False, f"unsupported REPL sandbox mode: {mode.value}"
            result = subprocess.run(  # noqa: S603
                argv,
                cwd=workspace,
                env=config.env_vars,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return False, f"sandboxed Python preflight failed: {exc}"
        if result.returncode == 0:
            return True, f"{mode.value} sandboxed Python preflight passed"
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = detail[-500:]
        else:
            detail = f"process exited with rc={result.returncode}"
        return False, f"sandboxed Python preflight failed: {detail}"


class KernelManager:
    """Maintain at most one persistent, half-duplex kernel per workspace."""

    def __init__(
        self,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        stdout_limit: int = DEFAULT_STDOUT_LIMIT,
    ) -> None:
        self.idle_timeout = idle_timeout
        self.stdout_limit = stdout_limit
        self._handles: dict[str, KernelHandle] = {}
        self._manager_lock = asyncio.Lock()

    async def _drain_stderr(self, handle: KernelHandle) -> None:
        stream = handle.process.stderr
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            handle.stderr_lines.append(text)
            del handle.stderr_lines[:-50]
            logger.debug("repl[%s] stderr: %s", handle.workspace_id, text)

    def _launch_argv(
        self,
        governor: Any,
        workspace: Path,
        temporary: Path,
    ) -> tuple[list[str], dict[str, str]]:
        config = _repl_sandbox_config(governor, workspace, temporary)
        # Preserve the venv launcher path. Resolving its symlink would make
        # Python lose ``pyvenv.cfg`` and therefore QwenPaw's site-packages.
        python = str(Path(sys.executable).absolute())
        child_argv = [python, "-m", "qwenpaw.repl.exec_server"]
        if config.mode is SandboxMode.SEATBELT:
            argv = _seatbelt_command(config, child_argv)
        elif config.mode is SandboxMode.BUBBLEWRAP:
            argv = _bubblewrap_command(config, child_argv)
        else:
            raise KernelUnavailableError(
                f"unsupported REPL sandbox mode: {config.mode.value}",
            )
        return argv, dict(config.env_vars)

    async def get_or_start(
        self,
        *,
        workspace_id: str,
        workspace: Path,
        specs: list[dict[str, Any]],
        governor: Any,
        session_id: str = "",
    ) -> KernelHandle:
        """Return the live session kernel, starting it lazily.

        Kernels are keyed by ``workspace_id`` plus a session hash
        (roadmap §2.5); an empty ``session_id`` keeps the legacy
        workspace-level key for compatibility.
        """
        available, reason = repl_sandbox_available(governor, preflight=True)
        if not available:
            raise KernelUnavailableError(
                f"CodeAct REPL unavailable (fail-closed): {reason}",
            )
        workspace = workspace.expanduser().resolve()
        key = _kernel_key(workspace_id, session_id)
        tag = _session_tag(session_id) if session_id else ""

        async with self._manager_lock:
            existing = self._handles.get(key)
            if existing is not None and existing.process.returncode is None:
                return existing
            if existing is not None:
                self._handles.pop(key, None)
                await self._terminate(existing)

            workspace.mkdir(parents=True, exist_ok=True)
            runtime_dir = workspace / ".qwenpaw-repl"
            temporary = runtime_dir / (f"tmp-{tag}" if tag else "tmp")
            temporary.mkdir(parents=True, exist_ok=True)
            (workspace / "out").mkdir(parents=True, exist_ok=True)

            argv, env = self._launch_argv(governor, workspace, temporary)
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workspace),
                    env=env,
                    start_new_session=True,
                    limit=MAX_KERNEL_MESSAGE_BYTES * 2,
                )
            except (OSError, ValueError) as exc:
                raise KernelUnavailableError(
                    f"failed to start sandboxed REPL: {exc}",
                ) from exc
            handle = KernelHandle(
                workspace_id=workspace_id,
                workspace=workspace,
                process=process,
                specs=[dict(item) for item in specs],
                specs_hash=_specs_hash(specs),
                session_id=str(session_id or ""),
                key=key,
            )
            handle.stderr_task = asyncio.create_task(
                self._drain_stderr(handle),
                name=f"repl-stderr-{key}",
            )
            self._handles[key] = handle
            await self._send(
                handle,
                {
                    "id": f"init-{uuid.uuid4().hex[:8]}",
                    "type": "init",
                    "tools": specs,
                    "config": {
                        "stdout_limit": self.stdout_limit,
                        "backend": resolve_backend_kind(),
                        "session_tag": tag or "default",
                    },
                },
            )
            await self._maybe_restore_snapshot(handle, tag or "default")
            self._schedule_reap(handle)
            return handle

    async def _maybe_restore_snapshot(
        self,
        handle: KernelHandle,
        session_tag: str,
    ) -> None:
        """Best-effort restore of the latest snapshot after a (re)start.

        Crash/reap recovery (roadmap §2.7): the kernel persists the user
        namespace after every successful cell, so a freshly started kernel
        can pick up where the previous one died.  Failures are logged and
        never block the first cell.
        """
        snapshot_dir = latest_snapshot_dir(handle.workspace, session_tag)
        if snapshot_dir is None:
            return
        restore_id = f"restore-{uuid.uuid4().hex[:8]}"
        snapshot_id = f"{session_tag}/latest"
        try:
            await self._send(
                handle,
                {
                    "id": restore_id,
                    "type": "restore",
                    "snapshot_id": snapshot_id,
                },
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + RESTORE_ACK_TIMEOUT
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                message = await self._read(handle, timeout=remaining)
                if message["type"] == "log":
                    continue
                if (
                    message["type"] == "restore_result"
                    and message["id"] == restore_id
                ):
                    if message.get("ok"):
                        logger.info(
                            "repl[%s] restored snapshot %s: %s",
                            handle.key,
                            snapshot_id,
                            message.get("restored"),
                        )
                    else:
                        logger.warning(
                            "repl[%s] snapshot restore failed: %s",
                            handle.key,
                            message.get("error"),
                        )
                    return
                raise ProtocolError(
                    "unexpected kernel message during restore: "
                    f"{message['type']}",
                )
        except (
            asyncio.TimeoutError,
            KernelCrashedError,
            OSError,
            ProtocolError,
        ) as exc:
            logger.warning(
                "repl[%s] snapshot restore skipped: %s",
                handle.key,
                exc,
            )

    async def _send(
        self, handle: KernelHandle, message: dict[str, Any]
    ) -> None:
        if (
            handle.process.stdin is None
            or handle.process.returncode is not None
        ):
            raise KernelCrashedError(self._crash_message(handle))
        # Tool results can legitimately contain multi-megabyte structured data.
        payload = encode_message(message)
        handle.process.stdin.write(payload)
        await handle.process.stdin.drain()

    async def _read(
        self,
        handle: KernelHandle,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if handle.process.stdout is None:
            raise KernelCrashedError(self._crash_message(handle))
        line = await asyncio.wait_for(
            handle.process.stdout.readline(),
            timeout=max(0.001, timeout),
        )
        if not line:
            await handle.process.wait()
            raise KernelCrashedError(self._crash_message(handle))
        return decode_message(line, max_bytes=MAX_KERNEL_MESSAGE_BYTES)

    def _crash_message(self, handle: KernelHandle) -> str:
        stderr = "\n".join(handle.stderr_lines[-10:])
        detail = f": {stderr}" if stderr else ""
        return (
            "[repl] kernel crashed/restarted, all variables lost"
            f" (exit={handle.process.returncode}){detail}"
        )

    async def _update_specs(
        self,
        handle: KernelHandle,
        specs: list[dict[str, Any]],
    ) -> None:
        stable_specs = _stable_tool_specs(handle.specs, specs)
        new_hash = _specs_hash(stable_specs)
        if new_hash == handle.specs_hash:
            return
        await self._send(
            handle,
            {
                "id": f"tools-{uuid.uuid4().hex[:8]}",
                "type": "tool_list_update",
                "tools": stable_specs,
            },
        )
        handle.specs = stable_specs
        handle.specs_hash = new_hash

    async def execute(
        self,
        handle: KernelHandle,
        code: str,
        bridge: GovernanceBridge,
        *,
        timeout: float = DEFAULT_EXEC_TIMEOUT,
        display: str = "summary",
    ) -> ExecResult:
        """Execute one cell and service its governed tool calls."""
        if not isinstance(code, str):
            raise TypeError("repl_exec code must be a string")
        from .output_policy import validate_display

        display = validate_display(display)
        async with handle.lock:
            if (
                handle.background_task is not None
                and not handle.background_task.done()
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(handle.background_task),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return ExecResult(
                        ok=False,
                        traceback=(
                            "[repl] a previous cell is still running after "
                            f"waiting {timeout:g}s; the kernel stays busy "
                            "until it finishes. If it will not finish soon, "
                            "call repl_exec(reset=True) to abandon it and "
                            "restart the kernel; state restores to the last "
                            "completed cell."
                        ),
                        error=make_error(
                            "failed",
                            code="kernel_busy",
                            message=(
                                "a previous timed-out cell is still running "
                                "in the background and is holding the kernel"
                            ),
                            retryable=True,
                            suggestion=(
                                "Do not resubmit the same code. If the "
                                "background work should finish soon, retry "
                                "once; otherwise call repl_exec(reset=True) "
                                "to abandon it and restart the kernel. State "
                                "restores to the last completed cell."
                            ),
                        ),
                    )
            handle.background_task = None
            if handle.process.returncode is not None:
                raise KernelCrashedError(self._crash_message(handle))
            if handle.reap_task is not None:
                handle.reap_task.cancel()
                handle.reap_task = None
            await self._update_specs(handle, bridge.specs)
            handle.cell_index += 1
            exec_id = f"e-{uuid.uuid4().hex[:12]}"
            started = time.monotonic()
            await self._send(
                handle,
                {
                    "id": exec_id,
                    "type": "exec",
                    "code": code,
                    "display": display,
                },
            )
            loop = asyncio.get_running_loop()
            deadline_holder = [loop.time() + timeout]
            read_semaphore = asyncio.Semaphore(READ_ONLY_CONCURRENCY)
            serial_lock = asyncio.Lock()
            read_only_check = getattr(bridge, "is_read_only", None)
            pending_tasks: set[asyncio.Task[None]] = set()
            tool_calls: list[dict[str, Any]] = []

            async def service_tool_call(
                message: dict[str, Any],
            ) -> None:
                call_started = loop.time()
                exposed = str(message.get("tool") or "")
                args = message.get("args")
                arguments = dict(args) if isinstance(args, dict) else {}
                status = "ok"
                response: dict[str, Any]
                try:
                    # Only tools whose descriptor explicitly opts in run
                    # concurrently (bounded); everything else stays serial.
                    if callable(read_only_check) and read_only_check(exposed):
                        async with read_semaphore:
                            value = await bridge.dispatch(
                                exposed,
                                arguments,
                                kernel_task_id=exec_id,
                            )
                    else:
                        async with serial_lock:
                            value = await bridge.dispatch(
                                exposed,
                                arguments,
                                kernel_task_id=exec_id,
                            )
                    response = {
                        "id": message["id"],
                        "type": "tool_result",
                        "ok": True,
                        "value": value,
                    }
                except ToolForwardingError as exc:
                    raw = str(exc)
                    kind, separator, detail = raw.partition("|")
                    error = classify_tool_error(
                        kind if separator else "failed",
                        detail if separator else raw,
                    )
                    status = error["kind"]
                    response = {
                        "id": message["id"],
                        "type": "tool_result",
                        "ok": False,
                        "error": error,
                    }
                except Exception as exc:  # noqa: BLE001
                    error = make_error(
                        "failed",
                        code=type(exc).__name__,
                        message=f"{type(exc).__name__}: {exc}",
                        retryable=False,
                    )
                    status = "failed"
                    response = {
                        "id": message["id"],
                        "type": "tool_result",
                        "ok": False,
                        "error": error,
                    }
                finally:
                    tool_calls.append(
                        {
                            "tool": exposed,
                            "status": status,
                            "duration_ms": round(
                                (loop.time() - call_started) * 1000,
                                1,
                            ),
                        },
                    )
                    # Nested approval may take minutes. The whole governed
                    # dispatch is excluded from the cell deadline rather than
                    # risking a timeout during ask.
                    deadline_holder[0] += loop.time() - call_started
                await self._send(handle, response)

            async def respond_interrupted(
                message: dict[str, Any],
            ) -> None:
                await self._send(
                    handle,
                    {
                        "id": message["id"],
                        "type": "tool_result",
                        "ok": False,
                        "error": make_error(
                            "interrupted",
                            message=(
                                "cell interrupt in progress; this tool call "
                                "was not executed"
                            ),
                        ),
                    },
                )

            try:
                while True:
                    remaining = deadline_holder[0] - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    message = await self._read(handle, timeout=remaining)
                    message_type = message["type"]
                    if message_type == "log":
                        logger.debug(
                            "repl[%s]: %s",
                            handle.key,
                            message.get("message"),
                        )
                        continue
                    if message_type == "tool_call":
                        task = asyncio.create_task(
                            service_tool_call(message),
                            name=f"repl-tool-{message.get('id')}",
                        )
                        pending_tasks.add(task)
                        task.add_done_callback(pending_tasks.discard)
                        continue
                    if (
                        message_type == "exec_result"
                        and message["id"] == exec_id
                    ):
                        if pending_tasks:
                            await asyncio.gather(
                                *pending_tasks,
                                return_exceptions=True,
                            )
                        handle.last_used = time.monotonic()
                        self._schedule_reap(handle)
                        result = ExecResult.from_message(message)
                        self._append_telemetry(
                            handle,
                            exec_id=exec_id,
                            ok=result.ok,
                            error_kind=(
                                result.error.get("kind")
                                if result.error
                                else ""
                            ),
                            duration_ms=round(
                                (time.monotonic() - started) * 1000,
                                1,
                            ),
                            tool_calls=tool_calls,
                            output_bytes=len(
                                result.stdout.encode(
                                    "utf-8",
                                    errors="replace",
                                ),
                            ),
                            spilled=list(result.spilled),
                            vars_delta=[
                                str(item.get("name"))
                                for item in result.vars_delta
                            ],
                            kernel_restarted=False,
                        )
                        return result
                    raise ProtocolError(
                        "unexpected kernel message during exec: "
                        f"{message_type}",
                    )
            except asyncio.TimeoutError:
                soft = await self._soft_interrupt(
                    handle,
                    exec_id,
                    loop=loop,
                    respond_interrupted=respond_interrupted,
                )
                if soft is not None:
                    self._append_telemetry(
                        handle,
                        exec_id=exec_id,
                        ok=False,
                        error_kind="interrupted",
                        duration_ms=round(
                            (time.monotonic() - started) * 1000,
                            1,
                        ),
                        tool_calls=tool_calls,
                        output_bytes=0,
                        spilled=[],
                        vars_delta=[],
                        kernel_restarted=False,
                    )
                    return soft
                # Detach instead of killing: the cell keeps running in the
                # background (variables retained) while a drain task consumes
                # its eventual exec_result. Killing here also deadlocked when
                # spawned children inherited the kernel pipes.
                handle.background_task = asyncio.create_task(
                    self._drain_background_cell(handle, exec_id),
                    name=f"repl-bg-{handle.key}",
                )
                self._append_telemetry(
                    handle,
                    exec_id=exec_id,
                    ok=False,
                    error_kind="timeout",
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    tool_calls=tool_calls,
                    output_bytes=0,
                    spilled=[],
                    vars_delta=[],
                    kernel_restarted=False,
                )
                return ExecResult(
                    ok=False,
                    traceback=(
                        f"[repl] execution timed out after {timeout:g}s; "
                        "the cell keeps running in the background and "
                        "variables are retained. If it will not finish "
                        "soon, call repl_exec(reset=True) to abandon it "
                        "and restart the kernel; state restores to the "
                        "last completed cell."
                    ),
                    error=make_error(
                        "timeout",
                        message=(
                            f"cell exceeded the {timeout:g}s budget but was "
                            "left running; the next repl_exec waits for it "
                            "to finish before executing"
                        ),
                        suggestion=(
                            "Do not resubmit the same code. If the work "
                            "should finish soon, wait and retry; otherwise "
                            "call repl_exec(reset=True) to abandon it and "
                            "restart the kernel."
                        ),
                    ),
                )

    async def _drain_background_cell(
        self,
        handle: KernelHandle,
        exec_id: str,
    ) -> None:
        """Consume messages until a detached cell's exec_result arrives."""
        try:
            while True:
                try:
                    message = await self._read(handle, timeout=3600.0)
                except asyncio.TimeoutError:
                    if handle.process.returncode is not None:
                        raise KernelCrashedError(
                            self._crash_message(handle),
                        )
                    continue
                message_type = message["type"]
                if message_type == "log":
                    continue
                if message_type == "tool_call":
                    await self._send(
                        handle,
                        {
                            "id": message["id"],
                            "type": "tool_result",
                            "ok": False,
                            "error": make_error(
                                "interrupted",
                                message=(
                                    "cell is running past its timeout; this "
                                    "tool call was not executed"
                                ),
                            ),
                        },
                    )
                    continue
                if message_type == "exec_result" and message["id"] == exec_id:
                    handle.last_used = time.monotonic()
                    self._schedule_reap(handle)
                    return
                raise ProtocolError(
                    "unexpected kernel message during background drain: "
                    f"{message_type}",
                )
        except (KernelCrashedError, ProtocolError, OSError) as exc:
            logger.warning(
                "repl[%s]: background cell drain aborted: %s",
                handle.key,
                exc,
            )
        finally:
            handle.background_task = None

    async def _soft_interrupt(
        self,
        handle: KernelHandle,
        exec_id: str,
        *,
        loop: asyncio.AbstractEventLoop,
        respond_interrupted: Any,
    ) -> ExecResult | None:
        """Try a soft interrupt; return a result or ``None`` to hard-kill.

        Level one of the §2.8 strategy: send ``interrupt`` and wait one
        grace period for the kernel's ``exec_result``. Tool calls arriving
        during the grace window are answered with ``kind=interrupted`` so
        the cell can unwind quickly.
        """
        try:
            await self._send(
                handle,
                {"id": exec_id, "type": "interrupt"},
            )
        except (KernelCrashedError, OSError):
            return None
        grace_deadline = loop.time() + SOFT_INTERRUPT_GRACE
        try:
            while True:
                remaining = grace_deadline - loop.time()
                if remaining <= 0:
                    return None
                message = await self._read(handle, timeout=remaining)
                message_type = message["type"]
                if message_type == "log":
                    continue
                if message_type == "tool_call":
                    await respond_interrupted(message)
                    continue
                if message_type == "exec_result" and message["id"] == exec_id:
                    handle.last_used = time.monotonic()
                    self._schedule_reap(handle)
                    return ExecResult(
                        ok=False,
                        traceback=(
                            "[repl] cell soft-interrupted; variables retained"
                        ),
                        error=make_error(
                            "interrupted",
                            message=(
                                "cell exceeded its budget and was "
                                "soft-interrupted; variables are retained"
                            ),
                        ),
                    )
                return None
        except (asyncio.TimeoutError, KernelCrashedError, ProtocolError):
            return None

    def _append_telemetry(
        self,
        handle: KernelHandle,
        *,
        exec_id: str,
        ok: bool,
        error_kind: str,
        duration_ms: float,
        tool_calls: list[dict[str, Any]],
        output_bytes: int,
        spilled: list[str],
        vars_delta: list[str],
        kernel_restarted: bool,
    ) -> None:
        """Append one per-cell observability record (roadmap §2.10)."""
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_id": handle.session_id,
            "workspace_id": handle.workspace_id,
            "exec_id": exec_id,
            "cell_index": handle.cell_index,
            "ok": ok,
            "error_kind": error_kind,
            "duration_ms": duration_ms,
            "tool_calls": list(tool_calls),
            "output_bytes": output_bytes,
            "spilled": list(spilled),
            "vars_delta": list(vars_delta),
            "kernel_restarted": kernel_restarted,
        }
        try:
            path = handle.workspace / ".qwenpaw-repl" / "telemetry.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle_file:
                handle_file.write(
                    json.dumps(record, ensure_ascii=False) + "\n",
                )
        except OSError:
            logger.debug(
                "repl[%s] telemetry write failed",
                handle.key,
                exc_info=True,
            )

    async def cancel(self, handle: KernelHandle) -> None:
        """Kill a running P0 kernel; all variables are intentionally lost."""
        await self._terminate(handle)
        async with self._manager_lock:
            if self._handles.get(handle.key) is handle:
                self._handles.pop(handle.key, None)

    async def reset_kernel(
        self,
        handle: KernelHandle,
        *,
        governor: Any,
        specs: list[dict[str, Any]] | None = None,
    ) -> KernelHandle:
        """Abandon a stuck kernel and restart a fresh one (roadmap §2.8).

        A runaway cell that survived the soft interrupt keeps the kernel
        permanently busy, trapping the caller in a ``kernel_busy`` loop.
        This is the escape hatch: it cancels the background drain, kills the
        kernel process, and restarts it. The restart restores the latest
        snapshot, so retained variables survive and only the stuck cell's
        partial work is lost. Returns the new handle.
        """
        background = handle.background_task
        if background is not None and not background.done():
            background.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await background
        handle.background_task = None
        await self._terminate(handle)
        async with self._manager_lock:
            if self._handles.get(handle.key) is handle:
                self._handles.pop(handle.key, None)
        return await self.get_or_start(
            workspace_id=handle.workspace_id,
            workspace=handle.workspace,
            specs=specs if specs is not None else handle.specs,
            governor=governor,
            session_id=handle.session_id,
        )

    async def _wait_exit(
        self,
        handle: KernelHandle,
        timeout: float,
    ) -> bool:
        """Wait for the kernel process to exit without requiring pipe EOF.

        ``Process.wait()`` also blocks until stdout/stderr reach EOF, which
        never happens when detached children inherit the kernel pipes
        (NO_PIDNS benchmark mode keeps such orphans alive by design).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while handle.process.returncode is None:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _terminate(self, handle: KernelHandle) -> None:
        if handle.reap_task is not None:
            handle.reap_task.cancel()
            handle.reap_task = None
        if handle.process.returncode is None:
            try:
                await self._send(
                    handle,
                    {
                        "id": f"shutdown-{uuid.uuid4().hex[:8]}",
                        "type": "shutdown",
                    },
                )
                if not await self._wait_exit(handle, 2.0):
                    with contextlib.suppress(ProcessLookupError):
                        handle.process.kill()
                    await self._wait_exit(handle, 2.0)
            except (BrokenPipeError, KernelCrashedError):
                with contextlib.suppress(ProcessLookupError):
                    handle.process.kill()
                await self._wait_exit(handle, 2.0)
        if handle.stderr_task is not None:
            handle.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.stderr_task

    def _schedule_reap(self, handle: KernelHandle) -> None:
        if self.idle_timeout <= 0:
            return
        if handle.reap_task is not None:
            handle.reap_task.cancel()

        async def reap() -> None:
            try:
                await asyncio.sleep(self.idle_timeout)
                if handle.lock.locked():
                    return
                if (
                    handle.background_task is not None
                    and not handle.background_task.done()
                ):
                    return
                await self._close_key(handle.key)
            except asyncio.CancelledError:
                return

        handle.reap_task = asyncio.create_task(
            reap(),
            name=f"repl-idle-reap-{handle.key}",
        )

    async def _close_key(self, key: str) -> None:
        """Shutdown and forget the kernel registered under ``key``."""
        async with self._manager_lock:
            handle = self._handles.pop(key, None)
        if handle is not None:
            await self._terminate(handle)

    async def close_workspace(self, workspace_id: str) -> None:
        """Shutdown and forget every session kernel of one workspace."""
        async with self._manager_lock:
            keys = [
                key
                for key, handle in self._handles.items()
                if handle.workspace_id == workspace_id
            ]
            handles = [self._handles.pop(key) for key in keys]
        await asyncio.gather(
            *(self._terminate(handle) for handle in handles),
            return_exceptions=True,
        )

    async def close_all(self) -> None:
        """Shutdown all kernels owned by this manager."""
        async with self._manager_lock:
            handles = list(self._handles.values())
            self._handles.clear()
        await asyncio.gather(
            *(self._terminate(handle) for handle in handles),
            return_exceptions=True,
        )


_DEFAULT_MANAGER: KernelManager | None = None


def get_default_kernel_manager() -> KernelManager:
    """Return the process-wide manager with isolated workspace kernels."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = KernelManager()
    return _DEFAULT_MANAGER


def register_repl_extra_mounts(governor: Any, mounts: list[Any]) -> None:
    """Attach extra sandbox mounts honoured at the next kernel launch.

    Other subsystems (e.g. scroll's recall backend, which needs a read-only
    view of the history store and a writable scratch dir inside the shared
    kernel) register mounts at build time; ``_repl_sandbox_config`` picks
    them up whenever the kernel (re)launches for this governor.
    """
    if governor is None:
        return
    # pylint: disable-next=protected-access
    governor._repl_extra_mounts = list(mounts)


__all__ = [
    "DEFAULT_EXEC_TIMEOUT",
    "DEFAULT_IDLE_TIMEOUT",
    "ExecResult",
    "KernelCrashedError",
    "KernelHandle",
    "KernelManager",
    "KernelUnavailableError",
    "get_default_kernel_manager",
    "register_repl_extra_mounts",
    "repl_sandbox_available",
]
