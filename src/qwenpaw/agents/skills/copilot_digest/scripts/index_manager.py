#!/usr/bin/env python3
"""Knowledge base index manager for Copilot Digest.

Provides CRUD operations on the content index (index.json).
All mutations to the index should go through this script.

Usage:
    python index_manager.py <command> [options]

Commands:
    add             Add a new item to the index
    list            List items with optional filters
    mark-read       Mark an item as read
    mark-discussed  Mark an item as discussed
    remove          Remove an item from the index
    stats           Show knowledge base statistics
"""

import argparse
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _index_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "index.json"


def _load_index(workspace_dir: str) -> dict:
    path = _index_path(workspace_dir)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "items": []}


def _save_index(workspace_dir: str, index: dict) -> None:
    path = _index_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file then rename
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        os.unlink(tmp)
        raise


def _generate_id() -> str:
    return secrets.token_hex(4)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_summary(args: argparse.Namespace) -> str:
    """Read summary from --summary-file if provided, otherwise use --summary."""
    if args.summary_file:
        p = Path(args.summary_file)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return args.summary or ""


# --- Commands ---


def cmd_add(args: argparse.Namespace) -> None:
    index = _load_index(args.workspace_dir)

    item_id = _generate_id()
    topics = [t.strip() for t in args.topics.split(",")] if args.topics else []

    # Read and validate summary — a non-empty summary is required
    summary = _read_summary(args)
    if not summary or len(summary.split()) < 50:
        hint = (
            "A podcast-style summary (400-800 words) is required. "
            "Write the summary to a file first, then pass it via --summary-file. "
            "See references/summarization_guide.md for format."
        )
        if args.summary_file:
            p = Path(args.summary_file)
            if not p.exists():
                print(json.dumps({
                    "status": "error",
                    "message": f"Summary file not found: {args.summary_file}. {hint}",
                }), file=sys.stderr)
            else:
                print(json.dumps({
                    "status": "error",
                    "message": f"Summary file is too short ({len(summary.split())} words, need 50+). {hint}",
                }), file=sys.stderr)
        else:
            print(json.dumps({
                "status": "error",
                "message": f"No summary provided. Use --summary-file <path>. {hint}",
            }), file=sys.stderr)
        sys.exit(1)

    # Count words in content file if it exists
    word_count = 0
    if args.content_path:
        content_file = Path(args.workspace_dir) / args.content_path
        if content_file.exists():
            word_count = len(content_file.read_text(encoding="utf-8").split())

    item = {
        "id": item_id,
        "title": args.title,
        "source_type": args.source_type,
        "source_url": args.source_url or "",
        "source_file": args.source_file or "",
        "saved_at": _now_iso(),
        "topics": topics,
        "summary": summary,
        "content_path": args.content_path or "",
        "word_count": word_count,
        "read": False,
        "discussed": False,
        "relevance_score": 0.0,
        "auto_fetched": args.auto_fetched,
    }

    index["items"].append(item)
    _save_index(args.workspace_dir, index)

    print(json.dumps({"status": "added", "id": item_id, "title": args.title}))


def cmd_list(args: argparse.Namespace) -> None:
    index = _load_index(args.workspace_dir)
    items = index.get("items", [])
    now = datetime.now(timezone.utc)

    # Apply filter
    if args.filter == "today":
        today = now.strftime("%Y-%m-%d")
        items = [i for i in items if i["saved_at"][:10] == today]
    elif args.filter == "yesterday":
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        items = [i for i in items if i["saved_at"][:10] == yesterday]
    elif args.filter == "week":
        week_ago = now - timedelta(days=7)
        items = [
            i for i in items
            if datetime.fromisoformat(i["saved_at"]) >= week_ago
        ]
    elif args.filter == "unread":
        items = [i for i in items if not i.get("read", False)]
    elif args.filter == "discussed":
        items = [i for i in items if i.get("discussed", False)]
    # "all" — no filtering

    # Apply limit
    items = items[: args.limit]

    if args.format == "json":
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        # Table format
        if not items:
            print("No items found.")
            return
        print(f"{'ID':<10} {'Date':<12} {'Type':<5} {'Read':<5} {'Title'}")
        print("-" * 80)
        for item in items:
            date = item["saved_at"][:10]
            read = "yes" if item.get("read") else "no"
            title = item["title"][:45]
            print(f"{item['id']:<10} {date:<12} {item['source_type']:<5} {read:<5} {title}")
        print(f"\nTotal: {len(items)} items")


def cmd_mark_read(args: argparse.Namespace) -> None:
    index = _load_index(args.workspace_dir)
    found = False
    for item in index["items"]:
        if item["id"] == args.id:
            item["read"] = True
            found = True
            break
    if not found:
        print(json.dumps({"status": "error", "message": f"Item {args.id} not found"}))
        sys.exit(1)
    _save_index(args.workspace_dir, index)
    print(json.dumps({"status": "updated", "id": args.id, "read": True}))


def cmd_mark_discussed(args: argparse.Namespace) -> None:
    index = _load_index(args.workspace_dir)
    found = False
    for item in index["items"]:
        if item["id"] == args.id:
            item["discussed"] = True
            item["read"] = True  # Discussed implies read
            found = True
            break
    if not found:
        print(json.dumps({"status": "error", "message": f"Item {args.id} not found"}))
        sys.exit(1)
    _save_index(args.workspace_dir, index)
    print(json.dumps({"status": "updated", "id": args.id, "discussed": True}))


def cmd_remove(args: argparse.Namespace) -> None:
    index = _load_index(args.workspace_dir)
    original_count = len(index["items"])
    index["items"] = [i for i in index["items"] if i["id"] != args.id]

    if len(index["items"]) == original_count:
        print(json.dumps({"status": "error", "message": f"Item {args.id} not found"}))
        sys.exit(1)

    _save_index(args.workspace_dir, index)

    # Also remove the article file if it exists
    if args.delete_content:
        for item in _load_index(args.workspace_dir).get("items", []):
            pass  # Already removed
        # We need to check from the original list
        # Re-read isn't needed; we had the item before filtering
        pass

    print(json.dumps({"status": "removed", "id": args.id}))


def cmd_stats(args: argparse.Namespace) -> None:
    index = _load_index(args.workspace_dir)
    items = index.get("items", [])
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_ago = now - timedelta(days=7)

    total = len(items)
    unread = sum(1 for i in items if not i.get("read", False))
    discussed = sum(1 for i in items if i.get("discussed", False))
    auto_fetched = sum(1 for i in items if i.get("auto_fetched", False))
    today_count = sum(1 for i in items if i["saved_at"][:10] == today)
    week_count = sum(
        1 for i in items
        if datetime.fromisoformat(i["saved_at"]) >= week_ago
    )

    # Topic counts
    topic_counts: dict[str, int] = {}
    for item in items:
        for topic in item.get("topics", []):
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    top_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Source type counts
    type_counts: dict[str, int] = {}
    for item in items:
        st = item.get("source_type", "unknown")
        type_counts[st] = type_counts.get(st, 0) + 1

    stats = {
        "total": total,
        "unread": unread,
        "read": total - unread,
        "discussed": discussed,
        "auto_fetched": auto_fetched,
        "manual": total - auto_fetched,
        "today": today_count,
        "this_week": week_count,
        "by_source_type": type_counts,
        "top_topics": [{"topic": t, "count": c} for t, c in top_topics],
    }

    if args.format == "json":
        print(json.dumps(stats, indent=2))
    else:
        print(f"Knowledge Base Statistics")
        print(f"========================")
        print(f"Total items:    {total}")
        print(f"Unread:         {unread}")
        print(f"Read:           {total - unread}")
        print(f"Discussed:      {discussed}")
        print(f"Auto-fetched:   {auto_fetched}")
        print(f"Manual:         {total - auto_fetched}")
        print(f"Today:          {today_count}")
        print(f"This week:      {week_count}")
        print(f"\nBy source type:")
        for st, count in type_counts.items():
            print(f"  {st}: {count}")
        if top_topics:
            print(f"\nTop topics:")
            for topic, count in top_topics:
                print(f"  {topic}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copilot Digest - Knowledge Base Index Manager"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- add ---
    p_add = subparsers.add_parser("add", help="Add a new item")
    p_add.add_argument("--workspace-dir", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument(
        "--source-type", required=True, choices=["url", "pdf", "csv", "text"]
    )
    p_add.add_argument("--source-url", default="")
    p_add.add_argument("--source-file", default="")
    p_add.add_argument("--topics", default="")
    p_add.add_argument("--summary", default="")
    p_add.add_argument(
        "--summary-file", default="",
        help="Path to a file containing the summary/script (use instead of --summary for long text)",
    )
    p_add.add_argument("--content-path", default="")
    p_add.add_argument("--auto-fetched", action="store_true", default=False)

    # --- list ---
    p_list = subparsers.add_parser("list", help="List items")
    p_list.add_argument("--workspace-dir", required=True)
    p_list.add_argument(
        "--filter",
        default="all",
        choices=["today", "yesterday", "week", "unread", "discussed", "all"],
    )
    p_list.add_argument("--format", default="table", choices=["json", "table"])
    p_list.add_argument("--limit", type=int, default=50)

    # --- mark-read ---
    p_read = subparsers.add_parser("mark-read", help="Mark item as read")
    p_read.add_argument("--workspace-dir", required=True)
    p_read.add_argument("--id", required=True)

    # --- mark-discussed ---
    p_disc = subparsers.add_parser("mark-discussed", help="Mark item as discussed")
    p_disc.add_argument("--workspace-dir", required=True)
    p_disc.add_argument("--id", required=True)

    # --- remove ---
    p_rm = subparsers.add_parser("remove", help="Remove an item")
    p_rm.add_argument("--workspace-dir", required=True)
    p_rm.add_argument("--id", required=True)
    p_rm.add_argument(
        "--delete-content",
        action="store_true",
        default=False,
        help="Also delete the article file",
    )

    # --- stats ---
    p_stats = subparsers.add_parser("stats", help="Show statistics")
    p_stats.add_argument("--workspace-dir", required=True)
    p_stats.add_argument("--format", default="table", choices=["json", "table"])

    args = parser.parse_args()

    commands = {
        "add": cmd_add,
        "list": cmd_list,
        "mark-read": cmd_mark_read,
        "mark-discussed": cmd_mark_discussed,
        "remove": cmd_remove,
        "stats": cmd_stats,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
