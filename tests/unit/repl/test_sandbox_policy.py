# -*- coding: utf-8 -*-
"""CodeAct-specific sandbox selection and command-policy tests."""

# pylint: disable=protected-access

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from qwenpaw.repl import kernel_manager
from qwenpaw.sandbox import SandboxCapability, SandboxMode


def test_codeact_can_select_container_bwrap_over_landlock(
    monkeypatch,
) -> None:
    governor = SimpleNamespace(
        sandbox_usable=True,
        sandbox_capability=SandboxCapability(
            supported=True,
            mode=SandboxMode.LANDLOCK,
            reason="Landlock ABI v6",
        ),
    )
    monkeypatch.setattr(kernel_manager.sys, "platform", "linux")
    monkeypatch.setattr(
        kernel_manager,
        "_probe_codeact_bubblewrap",
        lambda: (True, "container bwrap works"),
    )

    available, reason = kernel_manager.repl_sandbox_available(governor)

    assert available is True
    assert reason == "container bwrap works"


def test_codeact_bwrap_has_no_network_or_proc_mount(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.sandbox.bubblewrap_sandbox.BubblewrapSandbox._find_bwrap",
        lambda _self: "/usr/bin/bwrap",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = kernel_manager.SandboxConfig(
        mode=SandboxMode.BUBBLEWRAP,
        workspace_dir=str(workspace),
        mounts=[
            kernel_manager.MountSpec(
                path=str(workspace),
                writable=True,
                executable=False,
            ),
        ],
        allow_read_all=False,
        network_allow=[],
        env_vars={"PATH": ""},
    )

    command = (
        kernel_manager._bubblewrap_command(  # pylint: disable=protected-access
            config,
            ["/runtime/python", "-m", "qwenpaw.repl.exec_server"],
        )
    )

    assert "--unshare-net" in command
    assert "--unshare-pid" in command
    assert "--proc" not in command
    assert command[command.index("--remount-ro") + 1] == "/"
    assert "/bin" not in command
    assert "/usr/bin" not in command
    assert command[-3:] == [
        "/runtime/python",
        "-m",
        "qwenpaw.repl.exec_server",
    ]


def test_repl_sandbox_config_allow_read_all_escape_hatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    governor = SimpleNamespace(
        sandbox_usable=True,
        sandbox_capability=SandboxCapability(
            supported=True,
            mode=SandboxMode.BUBBLEWRAP,
            reason="bwrap available",
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()

    monkeypatch.delenv("QWENPAW_REPL_ALLOW_READ_ALL", raising=False)
    monkeypatch.delenv("QWENPAW_REPL_WRITABLE_PATHS", raising=False)
    monkeypatch.delenv("QWENPAW_REPL_NETWORK", raising=False)
    monkeypatch.delenv("QWENPAW_REPL_NO_PIDNS", raising=False)
    strict = kernel_manager._repl_sandbox_config(
        governor,
        workspace,
        temporary,
    )
    assert strict.allow_read_all is False
    assert strict.env_vars["PATH"] == ""
    assert strict.max_processes == 1
    assert strict.platform_hints["strict_workspace_only"] is True

    monkeypatch.setenv("QWENPAW_REPL_ALLOW_READ_ALL", "1")
    # Opt out of the default relaxed writable system dirs so the test keeps
    # asserting the minimal escape-hatch surface (workspace-only writable).
    monkeypatch.setenv("QWENPAW_REPL_WRITABLE_PATHS", "none")
    relaxed = kernel_manager._repl_sandbox_config(
        governor,
        workspace,
        temporary,
    )
    assert relaxed.allow_read_all is True
    assert "/usr/bin" in relaxed.env_vars["PATH"]
    assert relaxed.max_processes > 1
    assert relaxed.platform_hints["strict_workspace_only"] is False
    writable = [mount.path for mount in relaxed.mounts if mount.writable]
    assert writable == [str(workspace)]
