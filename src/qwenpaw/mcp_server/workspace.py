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
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


# ---------------------------------------------------------------------------
# mark_discussed
# ---------------------------------------------------------------------------

def mark_discussed(workspace_dir: Path, article_id: str) -> str:
    """Mark an item as discussed (also sets read=True). Returns JSON."""
    index = load_index(workspace_dir)
    for item in index.get("items", []):
        if item.get("id") == article_id:
            item["discussed"] = True
            item["read"] = True
            _save_index(workspace_dir, index)
            return json.dumps({
                "status": "updated",
                "id": article_id,
                "title": item.get("title", ""),
                "discussed": True,
                "read": True,
            })
    return f"Item '{article_id}' not found in the index."


# ---------------------------------------------------------------------------
# get_briefing  (ranked + grouped, no LLM needed)
# ---------------------------------------------------------------------------

_DECAY_RATE = 0.02  # per hour; half-life ~35 hours
_RECENCY_W = 0.4
_RELEVANCE_W = 0.4
_AUTHORITY_W = 0.2

_AUTHORITATIVE_DOMAINS = {
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "nytimes.com",
    "sec.gov", "federalreserve.gov", "fda.gov", "nih.gov",
    "nature.com", "science.org", "nejm.org", "lancet.com",
    "techcrunch.com", "wired.com", "arstechnica.com",
    "apnews.com", "bbc.com", "cnn.com", "theguardian.com",
    "politico.com", "thehill.com", "law360.com",
    "scotusblog.com", "statnews.com",
}


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        hostname = urlparse(url).hostname or ""
        return hostname[4:] if hostname.startswith("www.") else hostname
    except Exception:
        return ""


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _score_item(item: dict, config: dict, now: datetime) -> dict:
    saved = _parse_iso(item.get("saved_at", ""))
    hours = max(0, (now - saved).total_seconds() / 3600)
    recency = math.exp(-_DECAY_RATE * hours)

    profile = config.get("profile", {})
    config_terms: set[str] = set()
    for t in profile.get("topics", []):
        config_terms.update(t.lower().split())
    for sf in profile.get("sub_fields", []):
        config_terms.update(sf.lower().replace("_", " ").split())

    item_terms: set[str] = set()
    for t in item.get("topics", []):
        item_terms.update(t.lower().split())
    item_terms.update(item.get("title", "").lower().split())

    relevance = _jaccard(item_terms, config_terms)

    domain = _extract_domain(item.get("source_url", ""))
    authority = 1.0 if domain in _AUTHORITATIVE_DOMAINS else 0.5

    score = _RECENCY_W * recency + _RELEVANCE_W * relevance + _AUTHORITY_W * authority
    return {
        "score": round(score, 4),
        "recency": round(recency, 4),
        "relevance": round(relevance, 4),
        "authority": round(authority, 4),
    }


def get_briefing(
    workspace_dir: Path,
    *,
    timeframe: str = "all",
    group_by: str = "topic",
    filter_topic: str | None = None,
    filter_status: str = "all",
    top_n: int = 0,
) -> str:
    """Return a ranked briefing as formatted markdown.

    Parameters
    ----------
    timeframe : str
        ``"today"``, ``"yesterday"``, ``"week"``, ``"month"``, or ``"all"``.
    group_by : str
        ``"topic"``, ``"date"``, or ``"none"``.
    filter_topic : str | None
        Only items matching this topic.
    filter_status : str
        ``"unread"``, ``"read"``, ``"discussed"``, or ``"all"``.
    top_n : int
        Limit to top N items (0 = all).
    """
    index = load_index(workspace_dir)
    config = _load_config(workspace_dir)
    items: list[dict[str, Any]] = index.get("items", [])

    if not items:
        return "No items in the knowledge base yet."

    now = datetime.now(timezone.utc)

    # --- Timeframe filter ---
    if timeframe == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        items = [i for i in items if _parse_iso(i.get("saved_at", "")) >= cutoff]
    elif timeframe == "yesterday":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        items = [i for i in items if yesterday_start <= _parse_iso(i.get("saved_at", "")) < today_start]
    elif timeframe == "week":
        cutoff = now - timedelta(days=7)
        items = [i for i in items if _parse_iso(i.get("saved_at", "")) >= cutoff]
    elif timeframe == "month":
        cutoff = now - timedelta(days=30)
        items = [i for i in items if _parse_iso(i.get("saved_at", "")) >= cutoff]

    # --- Status filter ---
    if filter_status == "unread":
        items = [i for i in items if not i.get("read", False)]
    elif filter_status == "read":
        items = [i for i in items if i.get("read", False)]
    elif filter_status == "discussed":
        items = [i for i in items if i.get("discussed", False)]

    # --- Topic filter ---
    if filter_topic:
        tl = filter_topic.lower()
        items = [
            i for i in items
            if any(tl in t.lower() for t in i.get("topics", []))
            or tl in i.get("title", "").lower()
        ]

    if not items:
        return f"No items match (timeframe={timeframe}, status={filter_status})."

    # --- Score and sort ---
    scored = []
    for item in items:
        bd = _score_item(item, config, now)
        entry = {**item, "score_breakdown": bd}
        scored.append(entry)
    scored.sort(key=lambda x: x["score_breakdown"]["score"], reverse=True)

    for i, item in enumerate(scored, 1):
        item["rank"] = i

    if top_n > 0:
        scored = scored[:top_n]

    # --- Group ---
    groups: list[dict] | None = None
    if group_by == "topic":
        topic_groups: dict[str, list] = defaultdict(list)
        for item in scored:
            primary = (item.get("topics") or ["General"])[0]
            topic_groups[primary].append(item)
        groups = [
            {"topic": t, "items": g}
            for t, g in sorted(topic_groups.items(), key=lambda x: x[1][0]["score_breakdown"]["score"], reverse=True)
        ]
    elif group_by == "date":
        today_str = now.strftime("%Y-%m-%d")
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_groups: dict[str, list] = defaultdict(list)
        for item in scored:
            date_groups[item.get("saved_at", "")[:10]].append(item)
        groups = []
        for d in sorted(date_groups.keys(), reverse=True):
            if d == today_str:
                label = "Today"
            elif d == yesterday_str:
                label = "Yesterday"
            else:
                try:
                    label = datetime.strptime(d, "%Y-%m-%d").strftime("%A, %b %d")
                except ValueError:
                    label = d
            groups.append({"date": d, "label": label, "items": date_groups[d]})

    # --- Format ---
    return _format_briefing(scored, groups, group_by, timeframe, now)


def _format_briefing(
    ranked: list[dict],
    groups: list[dict] | None,
    group_by: str,
    timeframe: str,
    now: datetime,
) -> str:
    lines = [f"# Briefing — {timeframe.title()} ({now.strftime('%B %d, %Y')})", ""]

    if not ranked:
        lines.append("No items found for the selected criteria.")
        return "\n".join(lines)

    def _fmt_item(item: dict) -> list[str]:
        rank = item.get("rank", "?")
        pct = int(item.get("score_breakdown", {}).get("score", 0) * 100)
        tags = []
        if not item.get("read", False):
            tags.append("[unread]")
        if item.get("discussed", False):
            tags.append("[discussed]")
        if item.get("auto_fetched", False):
            tags.append("[auto-fetched]")
        domain = _extract_domain(item.get("source_url", "")) or item.get("source_type", "")
        summary = item.get("summary", "")
        out = [
            f"**{rank}. {item.get('title', '(untitled)')}**",
            f"   ID: {item.get('id', '?')} | Source: {domain} | Relevance: {pct}% {' '.join(tags)}",
        ]
        if summary:
            out.append(f"   {summary[:200]}{'...' if len(summary) > 200 else ''}")
        out.append("")
        return out

    if group_by == "topic" and groups:
        for g in groups:
            lines.append(f"## {g['topic']} ({len(g['items'])} item{'s' if len(g['items']) != 1 else ''})")
            lines.append("")
            for item in g["items"]:
                lines.extend(_fmt_item(item))
    elif group_by == "date" and groups:
        for g in groups:
            lines.append(f"## {g['label']} ({len(g['items'])} item{'s' if len(g['items']) != 1 else ''})")
            lines.append("")
            for item in g["items"]:
                lines.extend(_fmt_item(item))
    else:
        for item in ranked:
            lines.extend(_fmt_item(item))

    total = len(ranked)
    unread = sum(1 for i in ranked if not i.get("read", False))
    discussed = sum(1 for i in ranked if i.get("discussed", False))
    lines.append("---")
    lines.append(f"**Total:** {total} items | **Unread:** {unread} | **Discussed:** {discussed}")

    topic_counts: dict[str, int] = defaultdict(int)
    for item in ranked:
        for t in item.get("topics", []):
            topic_counts[t] += 1
    top = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    if top:
        lines.append(f"**Top topics:** {', '.join(f'{t} ({c})' for t, c in top)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------

def _load_config(workspace_dir: Path) -> dict[str, Any]:
    config_path = workspace_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read config.json: %s", exc)
        return {}


def _save_config(workspace_dir: Path, config: dict[str, Any]) -> None:
    config_path = workspace_dir / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(config_path))
    except Exception:
        os.unlink(tmp)
        raise


def get_config(workspace_dir: Path) -> str:
    """Return the user's config as formatted text."""
    config = _load_config(workspace_dir)
    if not config:
        return "No configuration found. The workspace has not been set up yet."

    lines = ["# Copilot Digest Configuration", ""]

    profile = config.get("profile", {})
    if profile:
        lines.append("## Profile")
        if profile.get("major_field"):
            lines.append(f"- **Field:** {profile['major_field']}")
        if profile.get("sub_fields"):
            lines.append(f"- **Sub-fields:** {', '.join(profile['sub_fields'])}")
        if profile.get("topics"):
            lines.append(f"- **Topics:** {', '.join(profile['topics'])}")
        if profile.get("role_title"):
            lines.append(f"- **Role:** {profile['role_title']}")
        lines.append("")

    sources = config.get("sources", [])
    if sources:
        lines.append("## Sources")
        for s in sources:
            status = "enabled" if s.get("enabled", True) else "disabled"
            lines.append(f"- {s.get('name', '?')} ({s.get('url', '')}) [{status}]")
        lines.append("")

    schedule = config.get("schedule", {})
    if schedule:
        lines.append("## Schedule")
        if schedule.get("fetch_cron"):
            lines.append(f"- **Fetch cron:** {schedule['fetch_cron']}")
        if schedule.get("max_items_per_fetch"):
            lines.append(f"- **Max items per fetch:** {schedule['max_items_per_fetch']}")
        lines.append("")

    prefs = config.get("preferences", {})
    if prefs:
        lines.append("## Preferences")
        for k, v in prefs.items():
            lines.append(f"- **{k.replace('_', ' ').title()}:** {v}")

    return "\n".join(lines)


def update_config(
    workspace_dir: Path,
    *,
    add_topics: list[str] | None = None,
    remove_topics: list[str] | None = None,
    add_sources: list[dict[str, str]] | None = None,
    remove_sources: list[str] | None = None,
    set_sub_fields: list[str] | None = None,
    set_summary_length: str | None = None,
    set_fetch_cron: str | None = None,
) -> str:
    """Update config.json with the given changes. Returns summary."""
    config = _load_config(workspace_dir)
    if not config:
        config = {"profile": {"topics": [], "sub_fields": []}, "sources": [], "preferences": {}, "schedule": {}}

    changes: list[str] = []
    profile = config.setdefault("profile", {})

    if add_topics:
        existing = profile.setdefault("topics", [])
        added = [t for t in add_topics if t not in existing]
        existing.extend(added)
        if added:
            changes.append(f"Added topics: {', '.join(added)}")

    if remove_topics:
        existing = profile.get("topics", [])
        removed = [t for t in remove_topics if t in existing]
        profile["topics"] = [t for t in existing if t not in remove_topics]
        if removed:
            changes.append(f"Removed topics: {', '.join(removed)}")

    if set_sub_fields is not None:
        profile["sub_fields"] = set_sub_fields
        changes.append(f"Set sub-fields: {', '.join(set_sub_fields)}")

    if add_sources:
        sources = config.setdefault("sources", [])
        existing_names = {s.get("name", "").lower() for s in sources}
        for src in add_sources:
            if src.get("name", "").lower() not in existing_names:
                sources.append({"name": src["name"], "url": src.get("url", ""), "enabled": True})
                changes.append(f"Added source: {src['name']}")

    if remove_sources:
        sources = config.get("sources", [])
        remove_lower = {n.lower() for n in remove_sources}
        removed = [s["name"] for s in sources if s.get("name", "").lower() in remove_lower]
        config["sources"] = [s for s in sources if s.get("name", "").lower() not in remove_lower]
        if removed:
            changes.append(f"Removed sources: {', '.join(removed)}")

    if set_summary_length:
        config.setdefault("preferences", {})["summary_length"] = set_summary_length
        changes.append(f"Set summary length: {set_summary_length}")

    if set_fetch_cron:
        config.setdefault("schedule", {})["fetch_cron"] = set_fetch_cron
        changes.append(f"Set fetch cron: {set_fetch_cron}")

    if not changes:
        return "No changes specified."

    _save_config(workspace_dir, config)
    return "Configuration updated:\n" + "\n".join(f"- {c}" for c in changes)


# ---------------------------------------------------------------------------
# save_work_output
# ---------------------------------------------------------------------------

def save_work_output(
    workspace_dir: Path,
    *,
    output_type: str,
    content: str,
    article_id: str | None = None,
) -> str:
    """Save a work output (notes, takeaways, action_items) to ``work/``.

    Parameters
    ----------
    output_type : str
        ``"notes"``, ``"takeaways"``, or ``"action_items"``.
    content : str
        Markdown content to save.
    article_id : str | None
        Optional article ID to associate with.

    Returns
    -------
    str
        JSON with status and file path.
    """
    work_dir = workspace_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    suffix = f"_{article_id}" if article_id else ""
    # Ensure unique filename
    base_name = f"{output_type}_{date_str}{suffix}.md"
    out_path = work_dir / base_name

    # If file exists, append a counter
    counter = 1
    while out_path.exists():
        base_name = f"{output_type}_{date_str}{suffix}_{counter}.md"
        out_path = work_dir / base_name
        counter += 1

    out_path.write_text(content, encoding="utf-8")
    rel_path = f"work/{base_name}"

    return json.dumps({
        "status": "saved",
        "file": rel_path,
        "output_type": output_type,
        "article_id": article_id,
        "date": date_str,
    })


# ---------------------------------------------------------------------------
# export_briefing
# ---------------------------------------------------------------------------

def export_briefing(
    workspace_dir: Path,
    *,
    item_ids: list[str] | None = None,
    include_all_work: bool = False,
    title: str | None = None,
) -> str:
    """Compile articles and work outputs into an export document.

    By default includes all read/discussed items from today plus today's
    work files. Returns the export as markdown text.
    """
    index = load_index(workspace_dir)
    items = index.get("items", [])
    now = datetime.now(timezone.utc)
    doc_title = title or f"Briefing - {now.strftime('%B %d, %Y')}"

    # Select articles
    selected: list[dict] = []
    if item_ids:
        id_set = set(item_ids)
        selected = [i for i in items if i["id"] in id_set]
    else:
        today = now.strftime("%Y-%m-%d")
        selected = [
            i for i in items
            if i.get("saved_at", "")[:10] == today
            and (i.get("read") or i.get("discussed"))
        ]

    # Collect work files
    work_entries: list[tuple[str, str]] = []
    work_dir = workspace_dir / "work"
    today_prefix = now.strftime("%Y-%m-%d")
    if work_dir.exists():
        for f in sorted(work_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                if include_all_work or today_prefix in f.name:
                    work_entries.append((f.name, f.read_text(encoding="utf-8")))

    # Build document
    sections: list[str] = [
        f"# {doc_title}", "",
        f"*Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}*", "",
    ]

    if selected:
        sections.append(f"## Article Summaries ({len(selected)})")
        sections.append("")
        for item in selected:
            sections.append(f"### {item['title']}")
            sections.append("")
            meta = []
            src = item.get("source_url") or item.get("source_type", "")
            if src:
                meta.append(f"**Source:** {src}")
            meta.append(f"**Saved:** {item.get('saved_at', '')[:10]}")
            if item.get("topics"):
                meta.append(f"**Topics:** {', '.join(item['topics'])}")
            sections.append(" | ".join(meta))
            sections.append("")
            if item.get("summary"):
                sections.append(item["summary"])
                sections.append("")
            # Include script content if available
            cp = item.get("content_path", "")
            if cp:
                script = _to_script_path(workspace_dir / cp)
                for p in ([script, workspace_dir / cp] if script else [workspace_dir / cp]):
                    if p and p.exists():
                        body = p.read_text(encoding="utf-8")
                        if len(body) > 3000:
                            body = body[:3000] + "\n\n*(content truncated for export)*"
                        sections.append(body)
                        sections.append("")
                        break
            sections.append("---")
            sections.append("")

    if work_entries:
        sections.append(f"## Work Outputs ({len(work_entries)})")
        sections.append("")
        for name, wc in work_entries:
            clean = name.replace(".md", "").replace("_", " ").title()
            sections.append(f"### {clean}")
            sections.append("")
            sections.append(wc)
            sections.append("")
            sections.append("---")
            sections.append("")

    # Extract action items
    action_items = []
    for _, wc in work_entries:
        for line in wc.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
                action_items.append(stripped)
    if action_items:
        sections.append(f"## Action Items ({len(action_items)})")
        sections.append("")
        sections.extend(action_items)
        sections.append("")

    sections.append("---")
    sections.append(f"*Copilot Digest | {now.strftime('%B %d, %Y')}*")

    # Save to exports/
    exports_dir = workspace_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    export_file = exports_dir / f"briefing_{now.strftime('%Y-%m-%d_%H%M')}.md"
    content_str = "\n".join(sections)
    export_file.write_text(content_str, encoding="utf-8")

    return (
        f"Export saved to: {export_file}\n\n"
        f"---\n\n{content_str}"
    )
