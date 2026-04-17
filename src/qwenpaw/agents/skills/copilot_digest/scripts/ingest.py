#!/usr/bin/env python3
"""Fixed two-phase ingestion pipeline for Copilot Digest.

Phase 1 — prepare:
    Extracts content from a file or URL, saves the article, archives the
    original, and prints JSON with the paths the agent needs.

Phase 2 — commit:
    Validates that the summary file exists and is adequate, then adds
    the item to the knowledge base index.

Usage:
    # Step 1: agent calls prepare
    python ingest.py prepare --workspace-dir <ws> --file <path>
    python ingest.py prepare --workspace-dir <ws> --url <url>

    # Step 2: agent reads article_path, writes summary to summary_path

    # Step 3: agent calls commit
    python ingest.py commit --workspace-dir <ws> --id <id> \
        --title "..." --topics "t1,t2" [--source-url <url>] [--auto-fetched]
"""

import argparse
import json
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Re-use extraction logic from extract_content.py (same directory)
# ---------------------------------------------------------------------------
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))
from extract_content import extract_from_url, extract_from_pdf, extract_from_csv, _format_article  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers (mirrors index_manager.py but kept self-contained)
# ---------------------------------------------------------------------------

def _load_index(workspace_dir: Path) -> dict:
    path = workspace_dir / "index.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "items": []}


def _save_index(workspace_dir: Path, index: dict) -> None:
    import os
    import tempfile
    path = workspace_dir / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        os.unlink(tmp)
        raise


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Phase 1: prepare
# ---------------------------------------------------------------------------

def cmd_prepare(args: argparse.Namespace) -> None:
    ws = Path(args.workspace_dir)
    articles_dir = ws / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    ingest_id = secrets.token_hex(4)
    date = _today()
    article_name = f"{date}_{ingest_id}.md"
    summary_name = f"{date}_{ingest_id}_script.md"
    article_path = articles_dir / article_name
    summary_path = articles_dir / summary_name

    # --- Detect source type & extract ---
    source_file_rel = ""
    source_type = ""

    if args.url:
        source_type = "url"
        content = extract_from_url(args.url, args.max_length)
    elif args.file:
        src = Path(args.file)
        if not src.exists():
            print(json.dumps({"status": "error", "message": f"File not found: {args.file}"}))
            sys.exit(1)

        ext = src.suffix.lower()
        if ext == ".pdf":
            source_type = "pdf"
            content = extract_from_pdf(args.file, args.max_length)
        elif ext in (".csv", ".tsv"):
            source_type = "csv"
            content = extract_from_csv(args.file)
        else:
            source_type = "text"
            text = src.read_text(encoding="utf-8", errors="replace")[:args.max_length]
            title = src.stem.replace("_", " ").replace("-", " ").title()
            content = _format_article(title, "", "text", text, source_file=src.name)

        # Archive original to inbox/processed/
        processed_dir = ws / "inbox" / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        dest = processed_dir / src.name
        # Avoid overwrite by appending id
        if dest.exists():
            dest = processed_dir / f"{src.stem}_{ingest_id}{src.suffix}"
        shutil.copy2(str(src), str(dest))
        source_file_rel = f"inbox/processed/{dest.name}"
    else:
        print(json.dumps({"status": "error", "message": "Either --file or --url is required"}))
        sys.exit(1)

    # --- Write article ---
    article_path.write_text(content, encoding="utf-8")

    # --- Output everything the agent needs ---
    result = {
        "status": "prepared",
        "ingest_id": ingest_id,
        "source_type": source_type,
        "source_file": source_file_rel,
        "source_url": args.url or "",
        "article_path": f"articles/{article_name}",
        "article_path_abs": str(article_path),
        "summary_path": f"articles/{summary_name}",
        "summary_path_abs": str(summary_path),
        "word_count": len(content.split()),
        "next_step": (
            "Read the article at article_path_abs. "
            "Write a 400-800 word podcast-style summary to summary_path_abs "
            "(see references/summarization_guide.md). "
            "Then call: python ingest.py commit --workspace-dir ... "
            f"--id {ingest_id} --title '...' --topics '...'"
        ),
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Phase 2: commit
# ---------------------------------------------------------------------------

def cmd_commit(args: argparse.Namespace) -> None:
    ws = Path(args.workspace_dir)
    date = _today()

    article_name = f"{date}_{args.id}.md"
    summary_name = f"{date}_{args.id}_script.md"
    article_path = ws / "articles" / article_name
    summary_path = ws / "articles" / summary_name

    # --- Validate article exists ---
    if not article_path.exists():
        print(json.dumps({
            "status": "error",
            "message": f"Article file not found: {article_path}. Run 'prepare' first.",
        }))
        sys.exit(1)

    # --- Validate summary exists and is adequate ---
    if not summary_path.exists():
        print(json.dumps({
            "status": "error",
            "message": (
                f"Summary file not found: {summary_path}. "
                "You must write the podcast-style summary before committing. "
                "See references/summarization_guide.md."
            ),
        }))
        sys.exit(1)

    summary = summary_path.read_text(encoding="utf-8").strip()
    summary_words = len(summary.split())
    if summary_words < 50:
        print(json.dumps({
            "status": "error",
            "message": (
                f"Summary is too short ({summary_words} words, need at least 50). "
                "Write a proper 400-800 word podcast-style summary. "
                "See references/summarization_guide.md."
            ),
        }))
        sys.exit(1)

    # --- Count words in article ---
    article_text = article_path.read_text(encoding="utf-8")
    word_count = len(article_text.split())

    # --- Determine source info ---
    # Try to recover source_file and source_url from prepare phase
    source_file = args.source_file or ""
    source_url = args.source_url or ""
    source_type = args.source_type

    # --- Build index entry ---
    topics = [t.strip() for t in args.topics.split(",")] if args.topics else []

    item = {
        "id": args.id,
        "title": args.title,
        "source_type": source_type,
        "source_url": source_url,
        "source_file": source_file,
        "saved_at": _now_iso(),
        "topics": topics,
        "summary": summary,
        "content_path": f"articles/{article_name}",
        "word_count": word_count,
        "read": False,
        "discussed": False,
        "relevance_score": 0.0,
        "auto_fetched": args.auto_fetched,
    }

    index = _load_index(ws)
    index["items"].append(item)
    _save_index(ws, index)

    print(json.dumps({
        "status": "added",
        "id": args.id,
        "title": args.title,
        "summary_words": summary_words,
        "article_words": word_count,
        "topics": topics,
    }, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copilot Digest — Fixed Ingestion Pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- prepare ---
    p_prep = subparsers.add_parser(
        "prepare",
        help="Phase 1: extract content and prepare for summarization",
    )
    p_prep.add_argument("--workspace-dir", required=True)
    p_prep.add_argument("--file", default=None, help="Local file (PDF, CSV, text)")
    p_prep.add_argument("--url", default=None, help="URL to fetch")
    p_prep.add_argument(
        "--max-length", type=int, default=50000,
        help="Max content length in characters (default: 50000)",
    )

    # --- commit ---
    p_commit = subparsers.add_parser(
        "commit",
        help="Phase 2: validate summary and add to index",
    )
    p_commit.add_argument("--workspace-dir", required=True)
    p_commit.add_argument("--id", required=True, help="Ingest ID from prepare phase")
    p_commit.add_argument("--title", required=True)
    p_commit.add_argument(
        "--source-type", required=True, choices=["url", "pdf", "csv", "text"],
    )
    p_commit.add_argument("--source-url", default="")
    p_commit.add_argument("--source-file", default="")
    p_commit.add_argument("--topics", default="")
    p_commit.add_argument("--auto-fetched", action="store_true", default=False)

    args = parser.parse_args()
    {"prepare": cmd_prepare, "commit": cmd_commit}[args.command](args)


if __name__ == "__main__":
    main()
