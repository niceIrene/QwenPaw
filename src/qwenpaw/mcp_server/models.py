# -*- coding: utf-8 -*-
"""Pydantic models for MCP tool responses.

These models back the ``structuredContent`` field in MCP tool results for
pure status-ACK tools (``mark_*``, ``save_work_output``). Display-oriented
tools (``list_*``, ``get_*``, ``update_config``, ``export_briefing``)
still return markdown strings because claude.ai renders those better for
humans than auto-serialized JSON.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StatusUpdate(BaseModel):
    """Result of marking an item's read / discussed status."""

    item_id: str = Field(description="The article ID that was updated.")
    title: str = Field(description="The article title at time of update.")
    read: bool
    discussed: bool


class SavedOutput(BaseModel):
    """Result of save_work_output."""

    output_type: Literal["notes", "takeaways", "action_items"]
    path: str = Field(
        description="Path of the saved file, relative to the workspace root.",
    )
    date: str = Field(description="Save date in YYYY-MM-DD format.")
    article_id: str | None = None
