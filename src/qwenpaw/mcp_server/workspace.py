# -*- coding: utf-8 -*-
"""Direct read/write access to the Copilot Digest workspace.

Bypasses the Copilot Digest assistant for fast, deterministic operations
on the content index and article files.  Used by the MCP server to expose
``list_reading_list``, ``get_article``, ``mark_read``, and ``get_stats``
tools that respond in milliseconds instead of round-tripping through
the assistant.

Write operations use the same atomic-write pattern as
``index_manager.py`` (tempfile + ``os.replace``).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------

def load_index(workspace_dir: Path) -> dict[str, Any]:
    """Read ``index.json`` and return the full index dict."""
    index_path = workspace_dir / "index.json"
    if not index_path.exists():
        return {"version": 1, "items": []}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read index.json: %s", exc)
        return {"version": 1, "items": []}


def _save_index(workspace_dir: Path, index: dict[str, Any]) -> None:
    """Atomic write: tempfile in same dir → ``os.replace``."""
    index_path = workspace_dir / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(index_path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(index_path))
    except Exception:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# list_items
# ---------------------------------------------------------------------------

def list_items(
    workspace_dir: Path,
    *,
    status: str = "unread",
    timeframe: str = "all",
    topic: str | None = None,
    limit: int = 50,
) -> str:
    """Return a formatted listing of knowledge-base items.

    Parameters
    ----------
    status : str
        ``"unread"`` (default), ``"read"``, or ``"all"``.
    timeframe : str
        ``"today"``, ``"yesterday"``, ``"week"``, ``"month"``, or ``"all"``.
    topic : str | None
        If set, only items whose topics include this string (case-insensitive).
    limit : int
        Maximum items to return (default 50).
    """
    index = load_index(workspace_dir)
    items: list[dict[str, Any]] = index.get("items", [])

    if not items:
        return "The reading list is empty."

    # --- status filter ---
    if status == "unread":
        items = [i for i in items if not i.get("read", False)]
    elif status == "read":
        items = [i for i in items if i.get("read", False)]

    # --- timeframe filter ---
    now = datetime.now(timezone.utc)
    if timeframe == "today":
        today = now.strftime("%Y-%m-%d")
        items = [i for i in items if i.get("saved_at", "")[:10] == today]
    elif timeframe == "yesterday":
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        items = [i for i in items if i.get("saved_at", "")[:10] == yesterday]
    elif timeframe == "week":
        week_ago = now - timedelta(days=7)
        items = [
            i for i in items
            if _parse_iso(i.get("saved_at", "")) >= week_ago
        ]
    elif timeframe == "month":
        month_ago = now - timedelta(days=30)
        items = [
            i for i in items
            if _parse_iso(i.get("saved_at", "")) >= month_ago
        ]

    # --- topic filter ---
    if topic:
        topic_lower = topic.lower()
        items = [
            i for i in items
            if any(topic_lower in t.lower() for t in i.get("topics", []))
        ]

    # Sort newest first
    items.sort(key=lambda i: i.get("saved_at", ""), reverse=True)

    if not items:
        return f"No items match (status={status}, timeframe={timeframe})."

    items = items[:limit]

    # Build structured output
    lines: list[str] = [f"Reading list ({len(items)} item(s)):"]
    for item in items:
        item_id = item.get("id", "?")
        title = item.get("title", "(untitled)")
        saved = item.get("saved_at", "")[:10]
        source = item.get("source_type", "")
        topics_str = ", ".join(item.get("topics", []))
        read_flag = "read" if item.get("read") else "unread"
        summary = item.get("summary", "")
        if len(summary) > 150:
            summary = summary[:147] + "..."

        lines.append(
            f"\n[{item_id}] {title}\n"
            f"  Saved: {saved} | Type: {source} | Status: {read_flag}\n"
            f"  Topics: {topics_str}\n"
            f"  Summary: {summary}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_article
# ---------------------------------------------------------------------------

def get_article(workspace_dir: Path, article_id: str) -> str:
    """Return the curated summary script of an article.

    Lookup order:
    1. Exact ID match
    2. Exact title match (case-insensitive)
    3. Partial title match (case-insensitive) — only if unambiguous

    File priority: ``_script.md`` (curated summary) first, then falls
    back to the raw article, then the inline summary in the index.
    """
    index = load_index(workspace_dir)
    items: list[dict[str, Any]] = index.get("items", [])

    match = _find_article(items, article_id)

    if match is None:
        # List available titles to help the user
        available = [
            f"  [{i.get('id')}] {i.get('title', '(untitled)')}"
            for i in items[:10]
        ]
        hint = "\n".join(available) if available else "  (empty)"
        return (
            f"No article found matching '{article_id}'.\n\n"
            f"Available articles:\n{hint}\n\n"
            "Use the exact ID or a more specific title."
        )

    content_path = match.get("content_path", "")
    title = match.get("title", "(untitled)")

    if content_path:
        # Prefer the _script.md (curated summary) over raw article
        script_path = _to_script_path(workspace_dir / content_path)
        raw_path = workspace_dir / content_path

        for path in (script_path, raw_path):
            if path is not None and path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    return f"# {title}\n\n{content}"
                except OSError as exc:
                    logger.warning("Error reading %s: %s", path, exc)

    # Last resort — inline summary from index
    summary = match.get("summary", "")
    if summary:
        return (
            f"# {title}\n\n"
            "(Article files not found — showing index summary)\n\n"
            f"{summary}"
        )
    return f"Article '{title}' has no content file or summary."


def _find_article(
    items: list[dict[str, Any]], query: str,
) -> dict[str, Any] | None:
    """Find an article by ID or title with decreasing specificity.

    Lookup order:
    1. Exact ID match
    2. Exact title match (case-insensitive)
    3. Substring match — query appears as-is in the title
    4. Keyword match — ALL words in the query appear in the title

    Steps 3 and 4 return a result only when exactly one item matches.
    If multiple items match, returns ``None`` so the caller can ask
    the user to be more specific.
    """
    # 1. Exact ID match
    for item in items:
        if item.get("id") == query:
            return item

    query_lower = query.lower()

    # 2. Exact title match (case-insensitive)
    for item in items:
        if item.get("title", "").lower() == query_lower:
            return item

    # 3. Substring match
    candidates = [
        i for i in items if query_lower in i.get("title", "").lower()
    ]
    if len(candidates) == 1:
        return candidates[0]

    # 4. Keyword match — all query words must appear in the title
    keywords = query_lower.split()
    if keywords:
        candidates = [
            i for i in items
            if all(kw in i.get("title", "").lower() for kw in keywords)
        ]
        if len(candidates) == 1:
            return candidates[0]

    return None


def _to_script_path(article_path: Path) -> Path | None:
    """Derive the ``_script.md`` path from an article path.

    ``articles/2026-04-16_a1b2c3d4.md`` → ``articles/2026-04-16_a1b2c3d4_script.md``
    """
    if article_path.suffix != ".md":
        return None
    return article_path.with_name(article_path.stem + "_script.md")


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------

def mark_read(workspace_dir: Path, article_id: str) -> str:
    """Mark an item as read. Returns confirmation or error string."""
    return _set_read_flag(workspace_dir, article_id, read=True)


def mark_unread(workspace_dir: Path, article_id: str) -> str:
    """Mark an item as unread. Returns confirmation or error string."""
    return _set_read_flag(workspace_dir, article_id, read=False)


def _set_read_flag(workspace_dir: Path, article_id: str, *, read: bool) -> str:
    """Set the ``read`` flag on an item."""
    index = load_index(workspace_dir)
    for item in index.get("items", []):
        if item.get("id") == article_id:
            item["read"] = read
            _save_index(workspace_dir, index)
            return json.dumps({
                "status": "updated",
                "id": article_id,
                "title": item.get("title", ""),
                "read": read,
            })
    return f"Item '{article_id}' not found in the index."


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

def get_stats(workspace_dir: Path) -> str:
    """Return knowledge-base statistics as a formatted string."""
    index = load_index(workspace_dir)
    items: list[dict[str, Any]] = index.get("items", [])

    if not items:
        return "Knowledge base is empty."

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_ago = now - timedelta(days=7)

    total = len(items)
    unread = sum(1 for i in items if not i.get("read", False))
    discussed = sum(1 for i in items if i.get("discussed", False))
    auto_fetched = sum(1 for i in items if i.get("auto_fetched", False))
    today_count = sum(1 for i in items if i.get("saved_at", "")[:10] == today)
    week_count = sum(
        1 for i in items if _parse_iso(i.get("saved_at", "")) >= week_ago
    )

    topic_counts = Counter(
        t for i in items for t in i.get("topics", [])
    )
    top_topics = topic_counts.most_common(5)

    type_counts = Counter(i.get("source_type", "unknown") for i in items)

    lines = [
        "Knowledge Base Statistics",
        "========================",
        f"Total items:    {total}",
        f"Unread:         {unread}",
        f"Read:           {total - unread}",
        f"Discussed:      {discussed}",
        f"Auto-fetched:   {auto_fetched}",
        f"Manual:         {total - auto_fetched}",
        f"Today:          {today_count}",
        f"This week:      {week_count}",
        "",
        "By source type:",
    ]
    for st, count in type_counts.items():
        lines.append(f"  {st}: {count}")
    if top_topics:
        lines.append("")
        lines.append("Top topics:")
        for topic_name, count in top_topics:
            lines.append(f"  {topic_name}: {count}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(iso_str: str) -> datetime:
    """Parse an ISO 8601 timestamp, falling back to epoch on failure."""
    if not iso_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)
