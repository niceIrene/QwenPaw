---
name: copilot_digest
description: "Helps researchers and professionals digest papers, articles, news, and blogs they don't have time to read. Collects, organizes, ranks, and summarizes content into a personal knowledge base, then delivers briefings and interactive discussions via chat."
metadata:
  builtin_skill_version: "1.0"
  qwenpaw:
    emoji: "📻"
    requires:
      env:
        - BRIEFER_INTERESTS
---

# Copilot Digest

A personal knowledge copilot for researchers and professionals. Ingests papers, articles, news, and blogs you don't have time to read — organizes, ranks, and summarizes them into a searchable knowledge base — then delivers briefings and interactive discussions via chat to help you stay on top of your field.

---

## 1. When to Activate

Use this skill when the user:

- **Sends a file** (PDF, DOCX, text) and wants it saved or summarized
- **Shares a URL** and asks to save, read, or track it
- **Asks for a briefing**: "what's new", "catch me up", "reading list", "briefing"
- **Wants to discuss or look up** a saved article or topic: "tell me about X", "what do I have on X", "discuss #3"
- **Wants to capture discussion output**: "save these notes", "action items from this paper", "write up what we discussed", "export today's briefing"
- **Configures interests**: "add fintech to my topics", "change my sources", "update my schedule"

Do NOT activate for general questions unrelated to the user's reading list, saved content, or configured interests.

---

## 1b. Communication Style — Audio-First Output

All responses MUST be suitable for audio playback (text-to-speech). Follow these rules in every message:

- **No emojis.** Never use emoji characters anywhere in output.
- **No tables.** Never use markdown tables. Present tabular information as natural-language sentences or short lists instead.
- **No ASCII art, box-drawing characters, or decorative lines** (no `━`, `─`, `═`, etc.).
- **Write in full, speakable sentences.** Avoid shorthand, symbols-as-words (e.g. write "and" not "&"), or dense formatting that sounds awkward when read aloud.
- **Use numbered or bulleted lists sparingly.** When listing items, prefer a conversational flow: "First, ... Second, ... Finally, ..."
- **Spell out abbreviations on first use** ("Securities and Exchange Commission, or SEC").
- **Avoid parenthetical asides that break sentence flow.** Rewrite as separate sentences instead.

---

## 2. First-Time Setup

If `config.json` does not exist in the workspace, run the onboarding flow described in `references/first_time_setup.md`. This walks the user through field selection, sub-fields, topics, sources, and schedule via multiple-choice questions, then saves the result to `config.json` and creates the workspace directories.

---

## 3. Content Ingestion

All ingestion goes through `scripts/ingest.py` — a fixed two-phase pipeline.
The script handles extraction, archiving, validation, and indexing.
Your only free-form task is writing the summary between the two phases.

### The three steps (every ingest, every time)

**Step 1 — Prepare** (script extracts content, archives original, returns paths):

```bash
cd {skill_dir} && python scripts/ingest.py prepare \
  --workspace-dir "<workspace_dir>" \
  --file "<source_file>"          # OR --url "<url>"
```

Output (JSON):
```json
{
  "status": "prepared",
  "ingest_id": "a1b2c3d4",
  "source_type": "pdf",
  "article_path_abs": "/path/to/articles/2026-04-16_a1b2c3d4.md",
  "summary_path_abs": "/path/to/articles/2026-04-16_a1b2c3d4_script.md",
  "next_step": "Read the article ... write summary ... then call commit"
}
```

**Step 2 — Write summary** (you do this):

Read the article at `article_path_abs`. Write a 400-800 word podcast-style summary following `references/summarization_guide.md`. Save it to `summary_path_abs`.

**Step 3 — Commit** (script validates summary and indexes):

```bash
cd {skill_dir} && python scripts/ingest.py commit \
  --workspace-dir "<workspace_dir>" \
  --id "a1b2c3d4" \
  --title "<title>" \
  --source-type pdf \
  --topics "topic1, topic2" \
  --source-file "inbox/processed/paper.pdf"   # or --source-url "<url>"
```

The commit will **fail** if the summary file is missing or under 50 words.

### 3.1 Chat Attachment (PDF, DOCX, Text Files)

1. `ingest.py prepare --file <path> ...`
2. Read article, write summary to `summary_path_abs`
3. `ingest.py commit --source-type <pdf|docx|text> --source-file ... ...`
4. Confirm to user

### 3.2 URL (Paste or Message)

1. `ingest.py prepare --url <url> ...`
2. Read article, write summary to `summary_path_abs`
3. `ingest.py commit --source-type url --source-url <url> ...`
4. Confirm to user

For **multiple URLs** in one message, process each sequentially.

### 3.3 Inbox Drop Folder (Cron)

Scan `inbox/` (excluding `processed/`). For each file, run Steps 1-3 from 3.1.

### 3.4 Auto-Fetch (Cron)

See Section 5. For each matching article found, run Steps 1-3 from 3.2 with `--auto-fetched`.

### After Ingestion — Confirm to User

```
Saved "<title>". Topics: <topics>. Relevance: <score> percent. <word_count> words. Added to today's reading list.
```

---

## 4. Interest Configuration

Users can update their profile at any time via conversation.

**Read current config:**
```bash
cat config.json
```

**Supported changes:**
- "Add crypto regulation to my topics" → append to `profile.topics`
- "Remove sports" → remove from `profile.topics`
- "Add Reuters as a source" → append to `sources`
- "Disable Bloomberg" → set `enabled: false` on matching source
- "Change sub-field to include IP law" → update `profile.sub_fields`
- "Switch to detailed briefings" → update `preferences.summary_length`
- "Update fetch schedule to every 2 hours" → update `schedule.fetch_cron` to `"0 */2 * * *"`

After updating, write back to `config.json` using `write_file`.

---

## 5. Auto-Enrichment (Cron Composition)

Set up a cron job to automatically fetch content. This uses the **cron** skill.

### News Fetch Job

```bash
qwenpaw cron create \
  --agent-id <your_agent_id> \
  --type agent \
  --name "briefer_auto_fetch" \
  --cron "<from config.schedule.fetch_cron>" \
  --channel <user_channel> \
  --target-user <user_id> \
  --target-session <session_id> \
  --text "Auto-fetch: Read config.json. For each enabled source, open the URL with browser_use, snapshot the page, extract new headlines. For each matching article, run the ingest pipeline: (1) ingest.py prepare --url <url>, (2) read article and write summary to summary_path_abs, (3) ingest.py commit --auto-fetched. Only notify me if 3 or more new high-relevance items are found."
```

### Inbox Scan Job

```bash
qwenpaw cron create \
  --agent-id <your_agent_id> \
  --type agent \
  --name "briefer_inbox_scan" \
  --cron "*/30 * * * *" \
  --channel <user_channel> \
  --target-user <user_id> \
  --target-session <session_id> \
  --text "Check inbox/ for new files. For each new PDF, DOCX, or text file: run the ingest pipeline: (1) ingest.py prepare --file <path>, (2) read article and write summary to summary_path_abs, (3) ingest.py commit. Files are archived to inbox/processed/ automatically by prepare."
```

### When a Fetch Job Runs

1. Read `config.json` for sources, topics, and keywords.
2. For each enabled source URL:
   - `browser_use` → open URL → snapshot → extract headlines
   - For each relevant article (matching topics/keywords):
     - `ingest.py prepare --url <article_url> ...`
     - Read article, write summary to `summary_path_abs`
     - `ingest.py commit --auto-fetched ...`
3. Limit to `max_items_per_fetch` new items per run.
4. If 3+ new items found with relevance > 70%, notify the user.

---

## 6. Briefing & Ranking

When the user asks for a briefing ("what's new", "catch me up", "reading list"):

### Generate Ranked List

```bash
cd {this_skill_dir} && python scripts/rank_and_summarize.py \
  --workspace-dir "<workspace_dir>" \
  --timeframe today \
  --group-by topic \
  --format markdown
```

Options:
- `--timeframe`: `today`, `yesterday`, `week`, `month`, `all`
- `--group-by`: `topic` (default), `date`, `none`
- `--filter-topic`: filter to a specific topic
- `--filter-status`: `unread`, `read`, `discussed`, `all`

### Present to User (Chat)

Format the output as a spoken-style briefing suitable for audio (see Section 1b). No emojis, no tables, no decorative lines. Example:

```
Here is your briefing for today, April 15th, 2026.

Starting with Securities and Enforcement. You have two items here.

Number one, and the top pick: "SEC Files Insider Trading Charges Against Former XYZ Exec." This comes from Reuters Legal with a relevance score of 92 percent. The SEC charged a former VP... This one is unread and was auto-fetched.

Number two: "New SPAC Disclosure Requirements Take Effect." From SEC.gov, relevance 78 percent. Enhanced disclosure rules... Also unread and auto-fetched.

Next up, Corporate and M&A. One item.

Number three: "Tech Giant Acquires AI Startup for $2B." From Bloomberg, relevance 71 percent.

To wrap up, you have 5 items total, 4 unread, and 1 already discussed. The top topics today are SEC enforcement with 2 items, M&A with 1, and Fintech with 1.
```

After presenting, ask: "Want to discuss any of these, or should I continue with a deeper dive on the top item?"

### Mark as Read

After the user reviews items, mark them:
```bash
cd {this_skill_dir} && python scripts/index_manager.py mark-read \
  --workspace-dir "<workspace_dir>" --id <item_id>
```

---

## 7. Interactive Discussion

When the user wants to discuss a specific item:

1. **Identify the item** — by number ("discuss number 3"), title ("tell me about the SEC case"), or topic ("anything on SPAC litigation?").
2. **Load full content** — read the article file from `articles/`.
3. **Engage in discussion** — answer questions, provide analysis, cross-reference with other saved articles when relevant.
4. **Track discussion** — mark items as discussed:
   ```bash
   cd {this_skill_dir} && python scripts/index_manager.py mark-discussed \
     --workspace-dir "<workspace_dir>" --id <item_id>
   ```

### Cross-Referencing

When discussing an item, check if other saved articles are related:
```bash
cd {this_skill_dir} && python scripts/index_manager.py list \
  --workspace-dir "<workspace_dir>" --filter all --format json
```
Look for items with overlapping topics and mention them: "This connects to an article from yesterday about..."

### Capturing Discussion Output

After a discussion, the user may want to save what came out of it — notes, takeaways, or action items. This is the natural end of the discuss flow, not a standalone task.

When the user asks to capture output (e.g. "save these notes", "what are the action items", "write up what we discussed"):

1. **Generate the output** based on the discussion that just happened:
   - **Discussion notes**: key points, insights, and questions raised during the conversation
   - **Takeaways**: the main conclusions or learnings from the paper
   - **Action items**: concrete next steps the user identified during discussion
2. **Save to `work/`** with a descriptive filename linking it to the source article:
   ```
   work/<type>_<date>_<item_id>.md
   ```
   Types: `notes`, `takeaways`, `action_items`
3. **Confirm** what was saved and remind the user they can export it later.

These work files accumulate over time and can be included in exports (see Section 8).

---

## 8. Export

When the user asks to export content:

### Generate Export

```bash
cd {this_skill_dir} && python scripts/export_summary.py \
  --workspace-dir "<workspace_dir>" \
  --item-ids "abc123,def456" \
  --work-files "notes_2026-04-15_abc123.md,action_items_2026-04-15_def456.md" \
  --format md \
  --output "<workspace_dir>/exports/briefing_2026-04-15.md" \
  --title "Daily Briefing - April 15, 2026"
```

Options:
- `--item-ids`: comma-separated article IDs to include
- `--work-files`: comma-separated work files to include
- `--include-all-work`: include all work files from today
- `--format`: `md` (default) or `txt`

### Deliver to User

Use `send_file_to_user` to deliver the export:
```
Here is your export for today. It includes 3 article summaries, 
discussion notes from 2 papers, and your action items.
```

### Export Without Explicit IDs

If the user says "export today's briefing" without specifying items:
1. Include all items from today that were read or discussed.
2. Include all work files created today.
3. Generate and send.

---

## 9. Knowledge Base Management

### Workspace Structure

```
<workspace>/
├── config.json              # User profile & interests
├── index.json               # Master content index (managed by scripts)
├── inbox/                   # Drop zone — user can add files here directly
│   └── processed/           # Originals moved here after processing
├── articles/                # Extracted content (one .md per item)
├── work/                    # Discussion output (notes, takeaways, action items)
└── exports/                 # Exported briefing documents
```

### Index Operations

All index operations go through `scripts/index_manager.py`. Do NOT manually edit `index.json`.

```bash
# List today's items
cd {this_skill_dir} && python scripts/index_manager.py list \
  --workspace-dir "<workspace_dir>" --filter today

# Show statistics
cd {this_skill_dir} && python scripts/index_manager.py stats \
  --workspace-dir "<workspace_dir>"

# Remove an item
cd {this_skill_dir} && python scripts/index_manager.py remove \
  --workspace-dir "<workspace_dir>" --id <item_id>
```

---

## 10. Quick Reference

| User Says | Agent Does |
|-----------|-----------|
| "Save this" + file | Run `ingest.py prepare` → read extracted article → write summary to `_script.md` → run `ingest.py commit` → confirm |
| "Save this: URL" | Run `ingest.py prepare --url` → read extracted article → write summary to `_script.md` → run `ingest.py commit` → confirm |
| "What's new today?" / "What's in my reading list?" | Read `index.json` → run `rank_and_summarize.py --timeframe today` → present ranked briefing grouped by topic |
| "This week's briefing" / "Catch me up" | Read `index.json` → run `rank_and_summarize.py --timeframe week` → present ranked briefing |
| "Unread items on fintech" | Read `index.json` → run `rank_and_summarize.py --filter-status unread --filter-topic fintech` → present filtered list |
| "Tell me about the SEC filing" / "What do I have on SPAC regulation?" | Look up item in `index.json` by topic or title → read pre-ingested `_script.md` from `articles/` → present summary → discuss → mark discussed |
| "Discuss #3" / "Tell me more about the Nvidia article" | Find item in `index.json` → read its `_script.md` from `articles/` → present and discuss → mark discussed |
| "Save these notes" / "Write up what we discussed" | Capture key points and insights from the current discussion → save to `work/notes_<date>_<id>.md` |
| "What are the action items from this paper?" | Extract action items from the discussion → save to `work/action_items_<date>_<id>.md` |
| "What are the takeaways?" | Summarize main conclusions from the discussion → save to `work/takeaways_<date>_<id>.md` |
| "Export today's briefing" | Run `export_summary.py` → compile articles + work → send file |
| "Add AI policy to my topics" | Update `config.json` interests |
| "Set up auto-fetch" | Create cron job via cron skill |
| "What sources am I tracking?" | Read and display `config.json` sources |
| "How many items do I have?" | Run `index_manager.py stats` |
