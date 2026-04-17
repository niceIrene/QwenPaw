# Content Ingestion Guide

All ingestion uses `scripts/ingest.py` — a fixed two-phase pipeline.
**Every ingest follows the same three steps. No exceptions.**

---

## Pre-flight

- `{skill_dir}` = the directory containing `scripts/` (the copilot_digest skill directory).
- `{workspace_dir}` = the copilot-digest workspace path (where `config.json` and `articles/` live).

---

## The Three Steps

### Step 1 — Prepare (script handles extraction + archiving)

For a **file** (PDF, CSV, text):
```bash
cd {skill_dir} && python scripts/ingest.py prepare \
  --workspace-dir "{workspace_dir}" \
  --file "<source_file_path>"
```

For a **URL**:
```bash
cd {skill_dir} && python scripts/ingest.py prepare \
  --workspace-dir "{workspace_dir}" \
  --url "<url>"
```

The script will:
- Extract content from the source
- Save the article to `articles/{date}_{id}.md`
- Archive the original file to `inbox/processed/` (file ingests only)
- Print JSON with `ingest_id`, `article_path_abs`, `summary_path_abs`, and `next_step`

### Step 2 — Write summary (you do this)

1. Read the extracted article at `article_path_abs` from the prepare output.
2. Write a 400-800 word podcast-style summary following `references/summarization_guide.md`.
3. Save it to `summary_path_abs` from the prepare output.

**This is the only free-form step. Do not skip it.**

### Step 3 — Commit (script validates summary + indexes)

```bash
cd {skill_dir} && python scripts/ingest.py commit \
  --workspace-dir "{workspace_dir}" \
  --id "<ingest_id from prepare output>" \
  --title "<title you generated>" \
  --source-type <pdf|csv|text|url> \
  --topics "<detected topics, comma-separated>" \
  --source-file "<source_file from prepare output>"   # file ingests
  --source-url "<url>"                                 # URL ingests
```

Add `--auto-fetched` for cron-sourced items.

The commit will **fail with an error** if:
- The summary file does not exist
- The summary is under 50 words

On success, prints JSON with `status: "added"`, `id`, `title`, `summary_words`, `article_words`.

### Step 4 — Confirm to user

```
Saved: "<title>"
  → <topics> | Relevance: <score>%
  → <word_count> words | Added to today's reading list
```

---

## Inbox Drop Folder

Scan `{workspace_dir}/inbox/` (excluding `processed/`). For each file, run Steps 1-4 above. The prepare phase handles archiving automatically.

---

## Auto-Fetch (Cron)

1. Read `config.json` for enabled sources, topics, and keywords.
2. For each enabled source URL, open with `browser_use` → snapshot → extract headlines.
3. For each headline matching user topics:
   - Run Steps 1-4 above with `--url` and `--auto-fetched`.
4. Limit to `max_items_per_fetch` per run.
5. Notify user only if 3+ new items with relevance > 70%.
