#!/usr/bin/env python3
"""Content extraction pipeline for Copilot Digest.

Extracts clean readable content from various source types into markdown files.

Usage:
    python extract_content.py --url "https://..." --output path/to/output.md
    python extract_content.py --file input.pdf --output path/to/output.md
    python extract_content.py --file input.csv --output path/to/output.md
"""

import argparse
import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path


def extract_from_url(url: str, max_length: int = 50000) -> str:
    """Fetch a web page and extract the main article content as markdown."""
    try:
        import requests
    except ImportError:
        print("Error: 'requests' package is required. Install with: pip install requests", file=sys.stderr)
        sys.exit(1)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    html = resp.text

    # Try readability-lxml first, fall back to BeautifulSoup
    title = ""
    body = ""

    try:
        from readability import Document
        doc = Document(html)
        title = doc.title()
        body = doc.summary()
        # Convert HTML to plain text
        body = _html_to_markdown(body)
    except ImportError:
        # Fall back to BeautifulSoup
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            print(
                "Error: 'beautifulsoup4' or 'readability-lxml' is required.\n"
                "Install with: pip install beautifulsoup4",
                file=sys.stderr,
            )
            sys.exit(1)

        soup = BeautifulSoup(html, "html.parser")

        # Extract title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Remove unwanted elements
        for tag in soup.find_all(["nav", "footer", "header", "aside", "script", "style", "noscript"]):
            tag.decompose()

        # Try to find article content
        article = (
            soup.find("article")
            or soup.find("main")
            or soup.find(class_=lambda c: c and ("article" in c or "content" in c or "post" in c))
            or soup.find("body")
        )

        if article:
            body = _soup_to_markdown(article)
        else:
            body = soup.get_text(separator="\n", strip=True)

    content = body[:max_length]

    return _format_article(title, url, "web", content)


def extract_from_pdf(file_path: str, max_length: int = 50000) -> str:
    """Extract text from a PDF file."""
    path = Path(file_path)
    if not path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n\n"
    except ImportError:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n\n"
        except ImportError:
            print(
                "Error: 'pdfplumber' or 'pypdf' is required.\n"
                "Install with: pip install pdfplumber",
                file=sys.stderr,
            )
            sys.exit(1)

    text = text[:max_length]
    title = path.stem.replace("_", " ").replace("-", " ").title()

    return _format_article(title, "", "pdf", text, source_file=path.name)


def extract_from_csv(file_path: str, max_rows: int = 100) -> str:
    """Extract content from a CSV file as a markdown table."""
    path = Path(file_path)
    if not path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return _format_article(path.stem, "", "csv", "(empty file)", source_file=path.name)

    header = rows[0]
    data_rows = rows[1 : max_rows + 1]
    total_rows = len(rows) - 1

    # Build markdown table
    lines = []
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in data_rows:
        # Pad or truncate row to match header length
        padded = row + [""] * (len(header) - len(row))
        padded = padded[: len(header)]
        lines.append("| " + " | ".join(padded) + " |")

    table = "\n".join(lines)

    summary_parts = [
        f"**Columns ({len(header)}):** {', '.join(header)}",
        f"**Total rows:** {total_rows}",
    ]
    if total_rows > max_rows:
        summary_parts.append(f"*(showing first {max_rows} rows)*")

    summary = "\n".join(summary_parts)
    content = f"{summary}\n\n{table}"

    title = path.stem.replace("_", " ").replace("-", " ").title()
    return _format_article(title, "", "csv", content, source_file=path.name)


def _html_to_markdown(html: str) -> str:
    """Simple HTML to plain text conversion."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        return _soup_to_markdown(soup)
    except ImportError:
        import re
        text = re.sub(r"<[^>]+>", "", html)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _soup_to_markdown(element) -> str:
    """Convert a BeautifulSoup element to simple markdown."""
    lines = []
    for child in element.children:
        if hasattr(child, "name"):
            if child.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                level = int(child.name[1])
                lines.append(f"\n{'#' * level} {child.get_text(strip=True)}\n")
            elif child.name == "p":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"\n{text}\n")
            elif child.name in ("ul", "ol"):
                for li in child.find_all("li", recursive=False):
                    lines.append(f"- {li.get_text(strip=True)}")
            elif child.name == "blockquote":
                text = child.get_text(strip=True)
                if text:
                    lines.append(f"\n> {text}\n")
            elif child.name in ("div", "section", "article"):
                lines.append(_soup_to_markdown(child))
        elif hasattr(child, "strip"):
            text = child.strip()
            if text:
                lines.append(text)

    return "\n".join(lines)


def _format_article(
    title: str,
    url: str,
    source_type: str,
    content: str,
    source_file: str = "",
) -> str:
    """Format extracted content as a markdown article."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# {title}", ""]

    if url:
        lines.append(f"**Source:** {url}")
    if source_file:
        lines.append(f"**File:** {source_file}")
    lines.append(f"**Type:** {source_type}")
    lines.append(f"**Extracted:** {now}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(content)

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copilot Digest - Content Extraction Pipeline"
    )
    parser.add_argument("--url", help="URL to fetch and extract")
    parser.add_argument("--file", help="Local file to extract (PDF or CSV)")
    parser.add_argument("--output", required=True, help="Output markdown file path")
    parser.add_argument(
        "--max-length",
        type=int,
        default=50000,
        help="Max content length in characters (default: 50000)",
    )

    args = parser.parse_args()

    if not args.url and not args.file:
        parser.error("Either --url or --file is required")

    if args.url:
        content = extract_from_url(args.url, args.max_length)
    else:
        file_path = Path(args.file)
        ext = file_path.suffix.lower()
        if ext == ".pdf":
            content = extract_from_pdf(args.file, args.max_length)
        elif ext in (".csv", ".tsv"):
            content = extract_from_csv(args.file)
        else:
            # Treat as plain text
            if not file_path.exists():
                print(f"Error: File not found: {args.file}", file=sys.stderr)
                sys.exit(1)
            text = file_path.read_text(encoding="utf-8", errors="replace")[: args.max_length]
            title = file_path.stem.replace("_", " ").replace("-", " ").title()
            content = _format_article(title, "", "text", text, source_file=file_path.name)

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    print(f"Extracted to: {args.output}")


if __name__ == "__main__":
    main()
