#!/usr/bin/env python3
"""Export generator for Copilot Digest.

Compiles selected articles and work outputs into a polished,
exportable markdown briefing document.

Usage:
    python export_summary.py --workspace-dir <path> [options]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _read_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def build_export(
    workspace_dir: str,
    item_ids: list[str] | None = None,
    work_files: list[str] | None = None,
    include_all_work: bool = False,
    title: str | None = None,
) -> str:
    """Build a complete export document."""
    workspace = Path(workspace_dir)
    briefer_dir = workspace
    index = _load_json(briefer_dir / "index.json")
    items = index.get("items", [])

    now = datetime.now(timezone.utc)
    doc_title = title or f"Briefing - {now.strftime('%B %d, %Y')}"

    sections: list[str] = []
    toc_entries: list[str] = []

    # --- Header ---
    sections.append(f"# {doc_title}")
    sections.append("")
    sections.append(f"*Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}*")
    sections.append("")

    # --- Collect articles ---
    selected_articles = []
    if item_ids:
        id_set = set(item_ids)
        selected_articles = [i for i in items if i["id"] in id_set]
    elif not work_files and not include_all_work:
        # Default: include all read or discussed items from today
        today = now.strftime("%Y-%m-%d")
        selected_articles = [
            i for i in items
            if i["saved_at"][:10] == today and (i.get("read") or i.get("discussed"))
        ]

    # --- Collect work files ---
    selected_work: list[tuple[str, str]] = []  # (filename, content)
    work_dir = briefer_dir / "work"

    if include_all_work:
        today_prefix = now.strftime("%Y-%m-%d")
        if work_dir.exists():
            for f in sorted(work_dir.iterdir()):
                if f.is_file() and f.suffix == ".md" and today_prefix in f.name:
                    selected_work.append((f.name, f.read_text(encoding="utf-8")))
    elif work_files:
        for wf in work_files:
            wf_path = work_dir / wf
            if wf_path.exists():
                selected_work.append((wf, wf_path.read_text(encoding="utf-8")))
            else:
                selected_work.append((wf, f"*(file not found: {wf})*"))

    # --- Build TOC ---
    section_num = 1
    if selected_articles:
        toc_entries.append(f"{section_num}. Article Summaries ({len(selected_articles)})")
        section_num += 1
    if selected_work:
        toc_entries.append(f"{section_num}. Work Outputs ({len(selected_work)})")
        section_num += 1

    # Check for action items in work files
    action_items = []
    for name, content in selected_work:
        if "action_items" in name.lower() or "- [ ]" in content or "- [x]" in content:
            # Extract checklist items
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
                    action_items.append(stripped)

    if action_items:
        toc_entries.append(f"{section_num}. Action Items ({len(action_items)})")

    if toc_entries:
        sections.append("## Table of Contents")
        sections.append("")
        for entry in toc_entries:
            sections.append(f"- {entry}")
        sections.append("")
        sections.append("---")
        sections.append("")

    # --- Article Summaries Section ---
    if selected_articles:
        sections.append("## Article Summaries")
        sections.append("")

        for item in selected_articles:
            sections.append(f"### {item['title']}")
            sections.append("")

            # Metadata line
            meta_parts = []
            source = item.get("source_url") or item.get("source_file") or item.get("source_type", "")
            if source:
                meta_parts.append(f"**Source:** {source}")
            meta_parts.append(f"**Saved:** {item['saved_at'][:10]}")
            if item.get("topics"):
                meta_parts.append(f"**Topics:** {', '.join(item['topics'])}")
            sections.append(" | ".join(meta_parts))
            sections.append("")

            # Summary
            if item.get("summary"):
                sections.append(item["summary"])
                sections.append("")

            # Full content if available
            content_path = item.get("content_path")
            if content_path:
                full_path = briefer_dir / content_path
                if full_path.exists():
                    content = full_path.read_text(encoding="utf-8")
                    # Skip the header (already have title) — find first ---
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        body = parts[2].strip()
                    else:
                        body = content
                    # Truncate very long content for export
                    if len(body) > 3000:
                        body = body[:3000] + "\n\n*(content truncated for export)*"
                    sections.append(body)
                    sections.append("")

            sections.append("---")
            sections.append("")

    # --- Work Outputs Section ---
    if selected_work:
        sections.append("## Work Outputs")
        sections.append("")

        for name, content in selected_work:
            # Use filename as section title (clean up)
            clean_name = name.replace(".md", "").replace("_", " ").title()
            sections.append(f"### {clean_name}")
            sections.append("")
            sections.append(content)
            sections.append("")
            sections.append("---")
            sections.append("")

    # --- Action Items Section ---
    if action_items:
        sections.append("## Action Items")
        sections.append("")
        for ai in action_items:
            sections.append(ai)
        sections.append("")

    # --- Footer ---
    sections.append("---")
    sections.append(f"*Copilot Digest | {now.strftime('%B %d, %Y')}*")

    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copilot Digest - Export Summary Generator"
    )
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument(
        "--item-ids",
        default="",
        help="Comma-separated article IDs to include",
    )
    parser.add_argument(
        "--work-files",
        default="",
        help="Comma-separated work file names to include",
    )
    parser.add_argument(
        "--include-all-work",
        action="store_true",
        default=False,
        help="Include all work files from today",
    )
    parser.add_argument(
        "--format",
        default="md",
        choices=["md", "txt"],
        help="Output format (default: md)",
    )
    parser.add_argument("--output", required=True, help="Output file path")
    parser.add_argument("--title", default=None, help="Document title")

    args = parser.parse_args()

    item_ids = [x.strip() for x in args.item_ids.split(",") if x.strip()] if args.item_ids else None
    work_files = [x.strip() for x in args.work_files.split(",") if x.strip()] if args.work_files else None

    content = build_export(
        workspace_dir=args.workspace_dir,
        item_ids=item_ids,
        work_files=work_files,
        include_all_work=args.include_all_work,
        title=args.title,
    )

    # For txt format, strip markdown
    if args.format == "txt":
        # Simple markdown stripping
        lines = []
        for line in content.split("\n"):
            line = line.lstrip("#").strip()
            line = line.replace("**", "").replace("*", "")
            lines.append(line)
        content = "\n".join(lines)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    print(f"Export saved to: {args.output}")


if __name__ == "__main__":
    main()
