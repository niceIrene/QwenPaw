# -*- coding: utf-8 -*-
"""Remote MCP server wrapping the Copilot Digest assistant over HTTP.

See ``src/qwenpaw/mcp_server/README.md`` and
``docs/design/copilot_digest_mcp.md`` for architecture and setup.
"""
from __future__ import annotations

from .cli import main

__all__ = ["main"]
