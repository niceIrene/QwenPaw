#!/usr/bin/env python3
"""Ranking and summarization engine for Copilot Digest.

Scores and ranks all items in the knowledge base index based on
configured interests and recency. Supports grouping by topic or date.

Usage:
    python rank_and_summarize.py --workspace-dir <path> [options]

Scoring formula:
    score = (recency_weight * recency) + (relevance_weight * relevance) + (authority_weight * authority)

    recency:   exp(-decay * hours_since_saved)
    relevance: jaccard(item_topics ∪ item_keywords, config_topics ∪ config_keywords)
    authority: 1.0 if source matches known authoritative sources, else 0.5
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Default scoring weights
DEFAULT_RECENCY_WEIGHT = 0.4
DEFAULT_RELEVANCE_WEIGHT = 0.4
DEFAULT_AUTHORITY_WEIGHT = 0.2
DECAY_RATE = 0.02  # Per hour; half-life ~35 hours

# Known authoritative source domains
AUTHORITATIVE_DOMAINS = {
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "nytimes.com",
    "sec.gov", "federalreserve.gov", "fda.gov", "nih.gov",
    "nature.com", "science.org", "nejm.org", "lancet.com",
    "techcrunch.com", "wired.com", "arstechnica.com",
    "apnews.com", "bbc.com", "cnn.com", "theguardian.com",
    "politico.com", "thehill.com", "law360.com",
    "scotusblog.com", "statnews.com",
}


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _parse_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def _extract_domain(url: str) -> str:
    """Extract the base domain from a URL."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Remove www. prefix
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except Exception:
        return ""


def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def score_item(item: dict, config: dict, now: datetime) -> dict:
    """Score a single item and return score breakdown."""
    # --- Recency ---
    saved_at = _parse_datetime(item["saved_at"])
    hours_elapsed = max(0, (now - saved_at).total_seconds() / 3600)
    recency = math.exp(-DECAY_RATE * hours_elapsed)

    # --- Relevance ---
    profile = config.get("profile", {})
    config_terms = set()
    for t in profile.get("topics", []):
        config_terms.update(t.lower().split())
    for sf in profile.get("sub_fields", []):
        config_terms.update(sf.lower().replace("_", " ").split())

    item_terms = set()
    for t in item.get("topics", []):
        item_terms.update(t.lower().split())
    # Also include words from the title
    item_terms.update(item.get("title", "").lower().split())

    relevance = _jaccard(item_terms, config_terms)

    # --- Authority ---
    domain = _extract_domain(item.get("source_url", ""))
    authority = 1.0 if domain in AUTHORITATIVE_DOMAINS else 0.5

    # --- Composite score ---
    score = (
        DEFAULT_RECENCY_WEIGHT * recency
        + DEFAULT_RELEVANCE_WEIGHT * relevance
        + DEFAULT_AUTHORITY_WEIGHT * authority
    )

    return {
        "score": round(score, 4),
        "recency": round(recency, 4),
        "relevance": round(relevance, 4),
        "authority": round(authority, 4),
    }


def rank_items(
    items: list[dict],
    config: dict,
    timeframe: str = "all",
    filter_topic: str | None = None,
    filter_status: str = "all",
) -> list[dict]:
    """Score, filter, and rank items."""
    now = datetime.now(timezone.utc)

    # --- Timeframe filter ---
    if timeframe == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        items = [i for i in items if _parse_datetime(i["saved_at"]) >= cutoff]
    elif timeframe == "yesterday":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        items = [
            i for i in items
            if yesterday_start <= _parse_datetime(i["saved_at"]) < today_start
        ]
    elif timeframe == "week":
        cutoff = now - timedelta(days=7)
        items = [i for i in items if _parse_datetime(i["saved_at"]) >= cutoff]
    elif timeframe == "month":
        cutoff = now - timedelta(days=30)
        items = [i for i in items if _parse_datetime(i["saved_at"]) >= cutoff]

    # --- Status filter ---
    if filter_status == "unread":
        items = [i for i in items if not i.get("read", False)]
    elif filter_status == "read":
        items = [i for i in items if i.get("read", False)]
    elif filter_status == "discussed":
        items = [i for i in items if i.get("discussed", False)]

    # --- Topic filter ---
    if filter_topic:
        topic_lower = filter_topic.lower()
        items = [
            i for i in items
            if any(topic_lower in t.lower() for t in i.get("topics", []))
            or topic_lower in i.get("title", "").lower()
        ]

    # --- Score and sort ---
    scored = []
    for item in items:
        breakdown = score_item(item, config, now)
        entry = {**item, "score_breakdown": breakdown}
        entry["relevance_score"] = breakdown["score"]
        scored.append(entry)

    scored.sort(key=lambda x: x["score_breakdown"]["score"], reverse=True)

    # Add rank
    for i, item in enumerate(scored, 1):
        item["rank"] = i

    return scored


def group_by_topic(items: list[dict]) -> list[dict]:
    """Group ranked items by their primary topic."""
    groups: dict[str, list] = defaultdict(list)
    for item in items:
        topics = item.get("topics", [])
        primary = topics[0] if topics else "General"
        groups[primary].append(item)

    # Sort groups by best item score
    result = []
    for topic, group_items in sorted(
        groups.items(), key=lambda x: x[1][0]["score_breakdown"]["score"], reverse=True
    ):
        result.append({"topic": topic, "items": group_items})

    return result


def group_by_date(items: list[dict]) -> list[dict]:
    """Group ranked items by date."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    groups: dict[str, list] = defaultdict(list)
    for item in items:
        date = item["saved_at"][:10]
        groups[date].append(item)

    result = []
    for date in sorted(groups.keys(), reverse=True):
        if date == today:
            label = "Today"
        elif date == yesterday:
            label = "Yesterday"
        else:
            label = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %b %d")
        result.append({"date": date, "label": label, "items": groups[date]})

    return result


def format_markdown(ranked: list[dict], groups: list[dict] | None, group_by: str, timeframe: str) -> str:
    """Format output as readable markdown."""
    now = datetime.now(timezone.utc)
    lines = [f"# Briefing — {timeframe.title()} ({now.strftime('%B %d, %Y')})", ""]

    total = len(ranked)
    unread = sum(1 for i in ranked if not i.get("read", False))
    discussed = sum(1 for i in ranked if i.get("discussed", False))

    if not ranked:
        lines.append("No items found for the selected criteria.")
        return "\n".join(lines)

    if group_by == "topic" and groups:
        for group in groups:
            topic = group["topic"]
            items = group["items"]
            lines.append(f"## {topic} ({len(items)} item{'s' if len(items) != 1 else ''})")
            lines.append("")
            for item in items:
                _append_item_lines(lines, item)
            lines.append("")
    elif group_by == "date" and groups:
        for group in groups:
            label = group["label"]
            items = group["items"]
            lines.append(f"## {label} ({len(items)} item{'s' if len(items) != 1 else ''})")
            lines.append("")
            for item in items:
                _append_item_lines(lines, item)
            lines.append("")
    else:
        for item in ranked:
            _append_item_lines(lines, item)
        lines.append("")

    lines.append("---")
    lines.append(f"**Total:** {total} items | **Unread:** {unread} | **Discussed:** {discussed}")

    # Top topics
    topic_counts: dict[str, int] = defaultdict(int)
    for item in ranked:
        for t in item.get("topics", []):
            topic_counts[t] += 1
    top = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    if top:
        topics_str = ", ".join(f"{t} ({c})" for t, c in top)
        lines.append(f"**Top topics:** {topics_str}")

    return "\n".join(lines)


def _append_item_lines(lines: list[str], item: dict) -> None:
    """Append formatted lines for a single item."""
    rank = item.get("rank", "?")
    score = item.get("score_breakdown", {}).get("score", 0)
    relevance_pct = int(score * 100)
    status_tags = []
    if not item.get("read", False):
        status_tags.append("unread")
    if item.get("discussed", False):
        status_tags.append("discussed")
    if item.get("auto_fetched", False):
        status_tags.append("auto-fetched")
    tags = " ".join(f"[{t}]" for t in status_tags)

    source = item.get("source_url", "") or item.get("source_file", "") or item.get("source_type", "")
    domain = _extract_domain(item.get("source_url", "")) or source

    lines.append(f"**{rank}. {item['title']}**")
    lines.append(f"   Source: {domain} | Relevance: {relevance_pct}% {tags}")
    if item.get("summary"):
        lines.append(f"   {item['summary']}")
    lines.append("")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copilot Digest - Ranking & Summarization Engine"
    )
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument(
        "--timeframe",
        default="all",
        choices=["today", "yesterday", "week", "month", "all"],
    )
    parser.add_argument(
        "--group-by", default="topic", choices=["topic", "date", "none"]
    )
    parser.add_argument("--filter-topic", default=None)
    parser.add_argument(
        "--filter-status",
        default="all",
        choices=["unread", "read", "discussed", "all"],
    )
    parser.add_argument("--top-n", type=int, default=0, help="Return only top N items (0=all)")
    parser.add_argument(
        "--format", default="json", choices=["json", "markdown"]
    )

    args = parser.parse_args()

    workspace = Path(args.workspace_dir)
    index = _load_json(workspace / "index.json")
    config = _load_json(workspace / "config.json")

    items = index.get("items", [])
    ranked = rank_items(
        items, config, args.timeframe, args.filter_topic, args.filter_status
    )

    if args.top_n > 0:
        ranked = ranked[: args.top_n]

    # Group
    groups = None
    if args.group_by == "topic":
        groups = group_by_topic(ranked)
    elif args.group_by == "date":
        groups = group_by_date(ranked)

    if args.format == "markdown":
        print(format_markdown(ranked, groups, args.group_by, args.timeframe))
    else:
        output = {
            "timeframe": args.timeframe,
            "total_items": len(ranked),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        if groups:
            output["groups"] = groups
        else:
            output["ranked_items"] = ranked

        # Stats
        output["stats"] = {
            "total": len(ranked),
            "unread": sum(1 for i in ranked if not i.get("read", False)),
            "discussed": sum(1 for i in ranked if i.get("discussed", False)),
        }

        # Clean up non-serializable data for JSON output
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
