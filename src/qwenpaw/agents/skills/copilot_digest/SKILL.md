---
name: copilot_digest
description: "Hands-free copilot that collects and organizes content (PDFs, CSVs, web URLs, news), delivers interactive briefings via chat or voice, discusses topics, and helps with simple tasks like drafting summaries, organizing notes, and writing action items."
metadata:
  builtin_skill_version: "1.0"
  qwenpaw:
    emoji: "📻"
    requires:
      env:
        - BRIEFER_INTERESTS
---

# Copilot Digest

A personal content copilot for professionals. Collects, organizes, ranks, summarizes, and discusses content — via chat or voice — and helps get simple work done hands-free.

---

## 1. When to Activate

Use this skill when the user:

- **Sends a file** (PDF, CSV, text) and wants it saved or summarized
- **Shares a URL** and asks to save, read, or track it
- **Asks for a briefing**: "what's new", "catch me up", "reading list", "briefing"
- **Wants to discuss** a previously saved article or topic
- **Requests work**: "draft a summary", "organize my notes", "action items", "write up", "case brief"
- **Configures interests**: "add fintech to my topics", "change my sources", "update my schedule"
- **Calls via voice** and expects a spoken briefing or discussion

Do NOT activate for general questions unrelated to the user's reading list, saved content, or configured interests.

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

### 3.1 Chat Attachment (PDF, CSV, Text Files)

1. `ingest.py prepare --file <path> ...`
2. Read article, write summary to `summary_path_abs`
3. `ingest.py commit --source-type <pdf|csv|text> --source-file ... ...`
4. Confirm to user

### 3.2 URL (Paste or Message)

1. `ingest.py prepare --url <url> ...`
2. Read article, write summary to `summary_path_abs`
3. `ingest.py commit --source-type url --source-url <url> ...`
4. Confirm to user

For **multiple URLs** in one message, process each sequentially.

### 3.3 Inbox Drop Folder

Scan `inbox/` (excluding `processed/`). For each file, run Steps 1-3 from 3.1.

### 3.4 Auto-Fetch (Cron)

See Section 5. For each matching article found, run Steps 1-3 from 3.2 with `--auto-fetched`.

### After Ingestion — Confirm to User

```
Saved: "<title>"
  → <topics> | Relevance: <score>%
  → <word_count> words | Added to today's reading list
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
  --text "Check inbox/ for new files. For each new PDF, CSV, or text file: run the ingest pipeline: (1) ingest.py prepare --file <path>, (2) read article and write summary to summary_path_abs, (3) ingest.py commit. Files are archived to inbox/processed/ automatically by prepare."
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

Format the output as a structured briefing:

```
📻 Briefing — Today (April 15, 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📂 Securities & Enforcement (2 items)

  1. ⭐ SEC Files Insider Trading Charges Against Former XYZ Exec
     Source: Reuters Legal | Relevance: 92%
     Summary: The SEC charged a former VP...
     [unread] [auto-fetched]

  2. New SPAC Disclosure Requirements Take Effect
     Source: SEC.gov | Relevance: 78%
     Summary: Enhanced disclosure rules...
     [unread] [auto-fetched]

📂 Corporate / M&A (1 item)

  3. Tech Giant Acquires AI Startup for $2B
     Source: Bloomberg | Relevance: 71%
     ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Stats: 5 items | 4 unread | 1 discussed
   Top topics: SEC enforcement (2), M&A (1), Fintech (1)
```

After presenting, ask: "Want to discuss any of these, or should I continue with a deeper dive on the top item?"

### Mark as Read

After the user reviews items, mark them:
```bash
cd {this_skill_dir} && python scripts/index_manager.py mark-read \
  --workspace-dir "<workspace_dir>" --id <item_id>
```

---

## 7. Voice Briefing Mode

When the conversation is happening over the **voice channel** (Twilio call), adapt the output for spoken delivery.

### Output Rules for Voice

- **No markdown** — no bullets, no bold, no headers
- **Short sentences** — natural spoken rhythm, conversational tone
- **Smooth transitions** — "Next up...", "Moving on to...", "The third item is..."
- **Numbers spoken naturally** — "about fifteen hundred" not "1,500"
- **Pause points** — after each item, ask "Want to discuss this, or shall I continue?"

### Voice Briefing Flow

```
Agent: "Good morning. You have 5 items in today's briefing, 
        across 3 topics. Starting with the most important.

        First: The SEC filed insider trading charges against a 
        former executive at XYZ Corp. This relates to trades 
        made right before the acquisition announcement. 
        Relevance score: ninety-two percent.

        Would you like to discuss this one, or should I move on?"

User:  "Tell me more."

Agent: [reads full article summary with analysis]

User:  "Draft a quick note about the key takeaway."

Agent: [drafts note, reads it back for approval]

User:  "Good. Next."

Agent: "Number two: New SPAC disclosure requirements took 
        effect today. This could impact the pending Delta 
        transaction..."
```

### End of Voice Briefing

- Summarize what was covered: "We covered 5 items today. You discussed 2 and I drafted 1 note."
- Offer to export: "Would you like me to save a summary you can review later?"
- Mark briefed items as read.

---

## 8. Interactive Discussion

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

---

## 9. Hands-Free Work Mode

When the user requests work output, detect the type and produce role-appropriate content.

### Detecting Work Intent

Trigger phrases:
- "Draft a summary of..."
- "Prepare a case brief..."
- "Organize my notes on..."
- "Write action items..."
- "Draft an email about..."
- "Compare articles X and Y..."

### Work Types and Output

| Request | Output Format | Saved To |
|---------|--------------|----------|
| Summary | Structured summary with key takeaways | `work/summary_{date}_{topic}.md` |
| Case brief | IRAC format (Issue, Rule, Application, Conclusion) | `work/brief_{date}_{topic}.md` |
| Notes | Structured outline with bullet points | `work/notes_{date}_{topic}.md` |
| Action items | Checklist with priorities | `work/action_items_{date}.md` |
| Email draft | Subject line + body | `work/email_{date}_{topic}.md` |
| Comparison | Side-by-side analysis | `work/comparison_{date}_{id}.md` |

### Role-Specific Formatting

Read `profile.major_field` from `config.json` and adapt:

- **Law**: Issue → Rule → Application → Conclusion (IRAC); legal citations; case comparisons; statute references
- **Finance**: Executive summary; key metrics and figures; risk/opportunity matrix; market implications
- **Media**: Inverted pyramid structure; key quotes highlighted; source attribution; angle suggestions
- **Technology**: Technical summary; architecture implications; comparison with alternatives; action items
- **Healthcare**: Clinical significance; regulatory implications; patient impact; evidence quality assessment
- **General**: Clean bullet points; key takeaways; next steps

### Voice Work Flow

When in voice mode:
1. Draft the content silently.
2. Read it back to the user in full.
3. Ask: "Would you like to change anything?"
4. Iterate based on verbal feedback.
5. Save the final version.

### Saving Work Output

Write the output using `write_file` to the appropriate path under `work/`. Confirm:
```
Saved: "Case Brief - XYZ Corp Insider Trading"
  → work/brief_2026-04-15_xyz_insider.md
  → Want me to export this, or keep working?
```

---

## 10. Export

When the user asks to export content:

### Generate Export

```bash
cd {this_skill_dir} && python scripts/export_summary.py \
  --workspace-dir "<workspace_dir>" \
  --item-ids "abc123,def456" \
  --work-files "brief_2026-04-15_xyz.md,action_items_2026-04-15.md" \
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
Here's your briefing export for today. It includes 3 article summaries, 
1 case brief, and your action items.
```

### Export Without Explicit IDs

If the user says "export today's briefing" without specifying items:
1. Include all items from today that were read or discussed.
2. Include all work files created today.
3. Generate and send.

---

## 11. Knowledge Base Management

### Workspace Structure

```
<workspace>/
├── config.json              # User profile & interests
├── index.json               # Master content index (managed by scripts)
├── inbox/                   # Drop zone — user can add files here directly
│   └── processed/           # Originals moved here after processing
├── articles/                # Extracted content (one .md per item)
├── work/                    # Work outputs (summaries, briefs, notes)
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

### Bookmarklet Setup

If the user asks for a way to save pages from their browser, generate a bookmarklet. Explain that they should create a new bookmark in their browser toolbar and paste this as the URL:

```
javascript:void(fetch('http://localhost:<PORT>/api/v1/agents/<AGENT_ID>/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'Save this URL to my reading list: '+location.href})}).then(()=>alert('Saved!')).catch(()=>alert('QwenPaw not running')))
```

Replace `<PORT>` and `<AGENT_ID>` with the actual values. The user can find these in their QwenPaw config.

---

## 12. Quick Reference

| User Says | Agent Does |
|-----------|-----------|
| "Save this" + file | Ingest file → extract → index → confirm |
| "Save this: URL" | Fetch URL → extract → index → confirm |
| "What's new today?" | Run rank_and_summarize → present briefing |
| "This week's briefing" | Briefing with `--timeframe week` |
| "Unread items on fintech" | `--filter-status unread --filter-topic fintech` |
| "Tell me about #3" | Load article → discuss → mark discussed |
| "Draft a summary" | Produce role-appropriate summary → save to work/ |
| "Organize my notes on X" | Structure notes → save to work/ |
| "Action items" | Generate checklist → save to work/ |
| "Export today's briefing" | Compile articles + work → send file |
| "Add AI policy to my topics" | Update config.json |
| "Set up auto-fetch" | Create cron job via cron skill |
| "What sources am I tracking?" | Read and display config.json sources |
| "How many items do I have?" | Run stats command |
