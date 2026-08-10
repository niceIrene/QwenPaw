"""End-to-end tests for the persistent OS-sandboxed kernel."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from qwenpaw.repl.governance_bridge import ToolForwardingError
from qwenpaw.repl.kernel_manager import (
    KernelCrashedError,
    KernelManager,
    _stable_tool_specs,
    repl_sandbox_available,
)
from qwenpaw.sandbox import probe_sandbox_support


class _NoToolBridge:
    specs: list[dict] = []

    async def dispatch(self, *_args, **_kwargs):
        raise AssertionError("no tool call was expected")


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
            "import socket\n" "socket.socket().connect(('127.0.0.1', 1))",
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
