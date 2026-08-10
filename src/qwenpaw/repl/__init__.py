# -*- coding: utf-8 -*-
"""Persistent CodeAct REPL for Coding Mode.

The P0 implementation follows ``design/qwenpaw-codeact-repl-design-doc.md``:
model-authored Python runs in a persistent, OS-sandboxed child process while
all tool calls return to the QwenPaw process for normal governance and
dispatch.
"""

from .kernel_manager import (
    ExecResult,
    KernelManager,
    get_default_kernel_manager,
    repl_sandbox_available,
)

__all__ = [
    "ExecResult",
    "KernelManager",
    "get_default_kernel_manager",
    "repl_sandbox_available",
]
