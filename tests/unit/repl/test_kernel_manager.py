# -*- coding: utf-8 -*-
"""End-to-end tests for the persistent OS-sandboxed kernel."""

# pylint: disable=too-many-statements

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from qwenpaw.repl.governance_bridge import ToolForwardingError
from qwenpaw.repl.kernel_manager import (
    KernelCrashedError,
    KernelManager,
    _repl_sandbox_config,
    _stable_tool_specs,
    register_repl_extra_mounts,
    repl_sandbox_available,
)
from qwenpaw.sandbox import probe_sandbox_support
from qwenpaw.sandbox.config import MountSpec, SandboxMode


class _NoToolBridge:
    specs: list[dict] = []

    async def dispatch(self, *_args, **_kwargs):
        raise AssertionError("no tool call was expected")


def test_repl_sandbox_config_includes_registered_extra_mounts(tmp_path):
    # Scroll's recall backend registers a read-only workspace view plus a
    # writable scratch dir on the governor; the kernel launch must honor them.
    governor = SimpleNamespace(
        sandbox_capability=SimpleNamespace(
            mode=SandboxMode.BUBBLEWRAP,
            reason="test",
        ),
    )
    register_repl_extra_mounts(
        governor,
        [
            MountSpec(path=str(tmp_path / "ws-root"), writable=False),
            MountSpec(path=str(tmp_path / ".scroll"), writable=True),
        ],
    )

    config = _repl_sandbox_config(governor, tmp_path / "ws", tmp_path / "tmp")

    by_path = {mount.path: mount for mount in config.mounts}
    assert by_path[str(tmp_path / "ws-root")].writable is False
    assert by_path[str(tmp_path / ".scroll")].writable is True


class _ToolBridge:
    specs = [
        {
            "path": "lookup",
            "name": "lookup",
            "description": "Look up one record.",
            "schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "path": "guarded_action",
            "name": "guarded_action",
            "description": "A policy-protected action.",
            "schema": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
    ]

    async def dispatch(self, tool, args, **_kwargs):
        if tool == "lookup":
            return {"query": args["query"], "count": 2}
        raise ToolForwardingError("denied|policy blocked this target")


def test_tool_updates_preserve_existing_prefix_and_append_new_tools() -> None:
    old = [
        {"path": "first", "description": "old first"},
        {"path": "removed", "description": "old removed"},
    ]
    incoming = [
        {"path": "first", "description": "updated first"},
        {"path": "new", "description": "new tool"},
    ]

    merged = _stable_tool_specs(old, incoming)

    assert [item["path"] for item in merged] == [
        "first",
        "removed",
        "new",
    ]
    assert merged[0]["description"] == "updated first"


@pytest.mark.asyncio
async def test_sandboxed_kernel_persists_state_and_blocks_escape(
    tmp_path,
) -> None:
    capability = probe_sandbox_support()
    governor = SimpleNamespace(
        sandbox_usable=capability.supported,
        sandbox_globally_enabled=True,
        sandbox_capability=capability,
    )
    available, reason = repl_sandbox_available(governor, preflight=True)
    if not available:
        pytest.skip(reason)
    manager = KernelManager(idle_timeout=0)
    try:
        handle = await manager.get_or_start(
            workspace_id="test-workspace",
            workspace=tmp_path,
            specs=[],
            governor=governor,
        )
        bridge = _NoToolBridge()

        first = await manager.execute(handle, "items = [1, 2, 3]", bridge)
        second = await manager.execute(handle, "sum(items)", bridge)
        outside_secret_path = tmp_path.parent / "outside-secret.txt"
        outside_secret_path.write_text("secret", encoding="utf-8")
        read_outside = await manager.execute(
            handle,
            f"open({str(outside_secret_path)!r}, encoding='utf-8').read()",
            bridge,
        )
        outside_write = await manager.execute(
            handle,
            "from pathlib import Path\n"
            "Path('../must-not-exist').write_text('escape')",
            bridge,
        )
        network = await manager.execute(
            handle,
            "import socket\n" + "socket.socket().connect(('127.0.0.1', 1))",
            bridge,
        )
        subprocess = await manager.execute(
            handle,
            "__import__('subprocess').run(['/bin/echo', 'escape'])",
            bridge,
        )
        (tmp_path / "outside-link").symlink_to(outside_secret_path)
        symlink_escape = await manager.execute(
            handle,
            "Path('outside-link').read_text()",
            bridge,
        )
        tool_bridge = _ToolBridge()
        roundtrip = await manager.execute(
            handle,
            "lookup_result = paw.tools.lookup(query='revenue')\n"
            "lookup_result['count']",
            tool_bridge,  # type: ignore[arg-type]
        )
        denied = await manager.execute(
            handle,
            "paw.tools.guarded_action(target='/outside')",
            tool_bridge,  # type: ignore[arg-type]
        )

        assert first.ok is True
        assert second.ok is True
        assert second.stdout.strip() == "6"
        assert read_outside.ok is False
        assert outside_write.ok is False
        assert not (tmp_path.parent / "must-not-exist").exists()
        assert network.ok is False
        assert subprocess.ok is False
        assert symlink_escape.ok is False
        assert roundtrip.ok is True
        assert roundtrip.stdout.strip() == "2"
        assert roundtrip.tool_trace == ({"tool": "lookup", "status": "ok"},)
        assert denied.ok is False
        assert "PawToolError" in denied.traceback
        assert "policy blocked this target" in denied.traceback

        handle.process.kill()
        await handle.process.wait()
        with pytest.raises(KernelCrashedError, match="all variables lost"):
            await manager.execute(handle, "items", bridge)

        restarted = await manager.get_or_start(
            workspace_id="test-workspace",
            workspace=tmp_path,
            specs=[],
            governor=governor,
        )
        fresh = await manager.execute(
            restarted,
            "'items' in globals()",
            bridge,
        )
        assert fresh.ok is True
        # Roadmap §2.7: the restarted kernel auto-restores the latest
        # snapshot, so retained variables survive a crash.
        assert fresh.stdout.strip() == "True"
        restored_items = await manager.execute(restarted, "items", bridge)
        assert restored_items.ok is True
        assert restored_items.stdout.strip() == "[1, 2, 3]"

        manager.idle_timeout = 0.05
        manager._schedule_reap(restarted)  # pylint: disable=protected-access
        await asyncio.sleep(0.15)
        assert restarted.process.returncode is not None
    finally:
        await manager.close_all()


@pytest.mark.asyncio
async def test_reset_kernel_abandons_stuck_background_cell(tmp_path) -> None:
    capability = probe_sandbox_support()
    governor = SimpleNamespace(
        sandbox_usable=capability.supported,
        sandbox_globally_enabled=True,
        sandbox_capability=capability,
    )
    available, reason = repl_sandbox_available(governor, preflight=True)
    if not available:
        pytest.skip(reason)
    manager = KernelManager(idle_timeout=0)
    try:
        handle = await manager.get_or_start(
            workspace_id="test-reset",
            workspace=tmp_path,
            specs=[],
            governor=governor,
        )
        bridge = _NoToolBridge()

        # Retain a variable so we can prove the restart restores state.
        seed = await manager.execute(handle, "answer = 41 + 1", bridge)
        assert seed.ok is True

        # A runaway cell that swallows KeyboardInterrupt survives the soft
        # interrupt, so it is detached and keeps the kernel busy.
        runaway = await manager.execute(
            handle,
            "import time\n"
            "while True:\n"
            "    try:\n"
            "        time.sleep(0.05)\n"
            "    except KeyboardInterrupt:\n"
            "        continue\n",
            bridge,
            timeout=1.0,
        )
        assert runaway.ok is False
        assert runaway.error is not None
        assert runaway.error["kind"] == "timeout"

        # While the runaway cell runs, the next cell reports kernel_busy.
        blocked = await manager.execute(handle, "1 + 1", bridge, timeout=1.0)
        assert blocked.ok is False
        assert blocked.error is not None
        assert blocked.error["code"] == "kernel_busy"

        # The escape hatch abandons the stuck cell and restarts the kernel.
        old_pid = handle.process.pid
        fresh = await manager.reset_kernel(handle, governor=governor)
        assert fresh.process.returncode is None
        assert fresh.process.pid != old_pid

        # The restarted kernel is usable and restored the retained variable.
        check = await manager.execute(fresh, "answer", bridge)
        assert check.ok is True
        assert check.stdout.strip() == "42"
    finally:
        await manager.close_all()
