# Copilot Digest - Design Document

> **Status:** Draft  
> **Author:** Lin, Yin  
> **Date:** 2026-04-15  
> **Skill Name:** `copilot_digest`  
> **Type:** Builtin Skill  

---

## 1. Problem Statement

Professionals - lawyers reviewing cases, finance analysts tracking markets, reporters organizing sources - spend significant "dead time" each day: commuting, waiting for flights, exercising, picking up kids. During these windows, they can't sit at a desk, but they *can* listen and talk.

Today, staying informed requires active screen time: reading newsletters, scanning RSS feeds, reviewing PDFs, and synthesizing takeaways. There's no way to:
- Have someone **collect and organize** your reading material automatically
- Get concise **summarizations** of lengthy documents, articles, and reports without reading them yourself
- **Listen to a briefing** of what matters most, hands-free
- **Discuss** a case or article with a knowledgeable partner while on the go
- **Get simple work done** - draft a summary, organize notes, write action items - by just talking

## 2. Solution Overview

**Copilot Digest** is a single QwenPaw builtin skill that transforms the agent into a personal information copilot. It:

1. **Collects** content from user-provided files (PDF, CSV) and URLs, plus auto-fetches news based on configured interests
2. **Organizes** a knowledge base ranked by relevance and recency
3. **Briefs** the user via chat or voice call, adapting delivery for the channel
4. **Discusses** any collected content interactively
5. **Works** on the user's behalf - drafting summaries, case briefs, notes, and action items through conversation

All of this is orchestrated by a single SKILL.md that leverages QwenPaw's existing tools and skills - no core code changes required.

---

## 3. Architecture

### 3.1 Why a Single Skill Is Sufficient

QwenPaw skills are Markdown instruction documents that guide the agent's behavior. They are not isolated code plugins. The copilot digest composes existing infrastructure:

| Capability | Provided By |
|-----------|-------------|
| Web content fetching | `browser_use` (built-in tool) |
| PDF extraction | `pdf` skill (existing) + pdfplumber |
| Scheduled fetching | `cron` skill (existing) |
| File I/O for knowledge base | `read_file`, `write_file`, `edit_file` (built-in tools) |
| Content delivery | `send_file_to_user` (built-in tool) |
| Data processing | `execute_shell_command` / `execute_python_code` (built-in tools) |
| Semantic search | `memory_search` (built-in tool) |

The skill adds: **domain instructions** (how to collect, organize, brief, discuss, and produce work output) + **utility scripts** (indexing, ranking, extraction, export).

### 3.2 Component Diagram

```
User (Chat / Voice Call)
        │
        ▼
┌─────────────────────────────┐
│     QwenPaw Agent           │
│  ┌───────────────────────┐  │
│  │  copilot_digest      │  │
│  │  SKILL.md (guidance)  │  │
│  └───────┬───────────────┘  │
│          │ orchestrates      │
│  ┌───────▼───────────────┐  │
│  │  Built-in Tools       │  │
│  │  - browser_use        │  │
│  │  - read/write_file    │  │
│  │  - execute_shell_cmd  │  │
│  │  - send_file_to_user  │  │
│  │  - memory_search      │  │
│  └───────┬───────────────┘  │
│          │ calls             │
│  ┌───────▼───────────────┐  │
│  │  scripts/             │  │
│  │  - index_manager.py   │  │
│  │  - extract_content.py │  │
│  │  - rank_summarize.py  │  │
│  │  - export_summary.py  │  │
│  └───────────────────────┘  │
│                              │
│  Composes with:              │
│  - cron skill (scheduling)   │
│  - pdf skill (extraction)    │
│  - news skill (sources)      │
└──────────────┬───────────────┘
               │
        ┌──────▼──────┐
        │  Workspace  │
        │  - config   │
        │  - index    │
        │  - articles │
        │  - work     │
        │  - exports  │
        └─────────────┘
```

### 3.3 Data Flow

```
                ┌──────────────────────────────────┐
                │          Content Sources          │
                ├─────────┬───────────┬────────────┤
                │  User   │   User    │   Auto     │
                │  Files  │   URLs    │   Fetch    │
                │(PDF/CSV)│(paste/msg)│  (cron)    │
                └────┬────┴─────┬─────┴──────┬─────┘
                     │          │             │
                     ▼          ▼             ▼
              ┌─────────────────────────────────────┐
              │     Content Extraction Pipeline      │
              │  (extract_content.py + browser_use)  │
              └──────────────────┬──────────────────┘
                                 │
                                 ▼
              ┌─────────────────────────────────────┐
              │        Knowledge Base (files)        │
              │  index.json ◄──► articles/*.md       │
              └──────────────────┬──────────────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ Briefing │ │Discussion│ │Work Mode │
              │ (ranked  │ │ (Q&A on  │ │ (drafts, │
              │  summary)│ │ articles)│ │  notes)  │
              └────┬─────┘ └────┬─────┘ └────┬─────┘
                   │            │             │
                   ▼            ▼             ▼
              ┌─────────────────────────────────────┐
              │           Export / Delivery          │
              │  (send_file_to_user / voice TTS)    │
              └─────────────────────────────────────┘
```

---

## 4. User Profiling & First-Time Setup

### 4.1 Onboarding Flow

On first interaction, the agent guides the user through a structured profiling process:

```
Agent: "Welcome to Copilot Digest! Let me set up your personal profile.
        First, what's your primary professional field?"

Step 1 — Major Field Selection:
  [ ] Law
  [ ] Finance & Banking
  [ ] Media & Journalism  
  [ ] Technology
  [ ] Healthcare & Pharma
  [ ] Government & Policy
  [ ] Academia & Research
  [ ] Other (specify)

Step 2 — Sub-Field Selection (based on major field):
  [If Law]:
    [ ] Corporate / M&A
    [ ] Securities & Capital Markets
    [ ] Litigation & Dispute Resolution
    [ ] Intellectual Property
    [ ] Employment & Labor
    [ ] Regulatory & Compliance
    [ ] Criminal Law
    [ ] International Trade
    
  [If Finance & Banking]:
    [ ] Investment Banking
    [ ] Asset Management / Portfolio
    [ ] Private Equity / Venture Capital
    [ ] Fintech & Digital Payments
    [ ] Risk & Compliance
    [ ] Crypto & Digital Assets
    [ ] Insurance
    [ ] Macroeconomics & Central Banking
    
  [If Media & Journalism]:
    [ ] Tech & Innovation
    [ ] Politics & Policy
    [ ] Business & Markets
    [ ] Science & Environment
    [ ] Culture & Society
    [ ] Investigative
    [ ] International Affairs
    
  [If Technology]:
    [ ] AI & Machine Learning
    [ ] Cloud & Infrastructure
    [ ] Cybersecurity
    [ ] Product Management
    [ ] Software Engineering
    [ ] Data Science
    [ ] Developer Tools
    
  [If Healthcare & Pharma]:
    [ ] Drug Development & Clinical Trials
    [ ] Medical Devices
    [ ] Health Policy & Regulation
    [ ] Digital Health
    [ ] Biotechnology
    
  [If Government & Policy]:
    [ ] Domestic Policy
    [ ] Foreign Affairs & Diplomacy
    [ ] Trade & Economics
    [ ] Defense & National Security
    [ ] Environmental & Climate
    
  [If Academia & Research]:
    [ ] Computer Science
    [ ] Economics
    [ ] Social Sciences
    [ ] Natural Sciences
    [ ] Humanities

Step 3 — Specific Topics & Keywords:
  Agent: "Now, list any specific topics, companies, regulations, 
          or keywords you want to track. For example:
          - 'SEC enforcement actions'
          - 'NVIDIA earnings'
          - 'EU AI Act'
          - 'SPAC litigation'"
  User provides free-text list → agent extracts and normalizes keywords

Step 4 — Preferred Sources:
  Agent suggests default sources based on field + sub-fields:
  
  [Law defaults]:
    Reuters Legal, Law360, SEC EDGAR, SCOTUS Blog, 
    National Law Review, Bloomberg Law
    
  [Finance defaults]:
    Bloomberg, Financial Times, Reuters, WSJ, 
    CNBC, Seeking Alpha, Federal Reserve
    
  [Media defaults]:
    AP News, Reuters, Nieman Lab, CJR, 
    Poynter, Press Gazette
    
  [Technology defaults]:
    TechCrunch, The Verge, Ars Technica, Hacker News,
    MIT Technology Review, Wired
    
  [Healthcare defaults]:
    STAT News, BioPharma Dive, FDA.gov, 
    New England Journal of Medicine, Nature Medicine
    
  User can accept defaults, add more, or remove any.

Step 5 — Schedule & Preferences:
  Agent: "How often should I check for new content?"
  Options: Every 2 hours / Every 4 hours / Every 6 hours (default) / Twice daily / Daily
  
  Agent: "Preferred briefing length?"
  Options: Brief (key headlines + 1-line each) / Standard (paragraph summaries) / Detailed (full analysis)
```

### 4.2 Profile Data Model (config.json)

```json
{
  "profile": {
    "major_field": "law",
    "sub_fields": ["securities_capital_markets", "corporate_ma"],
    "topics": ["SEC enforcement", "SPAC litigation", "proxy fights", "insider trading"],
    "role_title": "Securities lawyer"
  },
  "sources": [
    {"name": "Reuters Legal", "url": "https://www.reuters.com/legal/", "enabled": true},
    {"name": "SEC EDGAR 8-K", "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=8-K&dateb=&owner=include&count=40", "enabled": true},
    {"name": "Bloomberg Law", "url": "https://news.bloomberglaw.com/", "enabled": true},
    {"name": "SCOTUS Blog", "url": "https://www.scotusblog.com/", "enabled": false}
  ],
  "schedule": {
    "fetch_cron": "0 */6 * * *",
    "max_items_per_fetch": 10
  },
  "preferences": {
    "language": "en",
    "summary_length": "standard",
    "briefing_style": "professional"
  }
}
```

### 4.3 Profile Updates

Users can modify their profile at any time via conversation:
- "Add crypto regulation to my topics"
- "Remove Bloomberg from my sources"
- "Change my sub-field to include IP law"
- "Switch to detailed briefings"
- "Update my fetch schedule to every 2 hours"

The agent reads the current `config.json`, applies the change, and writes it back.

---

## 5. Summarization Organization

### 5.1 How Users Load Summarizations

Users access their reading list and summaries through natural conversation:

**By date:**
- "What's new today?" → Items from today
- "This week's briefing" → Items from past 7 days
- "Show me last week's items" → Items from 7-14 days ago
- "What did I save on Monday?" → Items from a specific date

**By topic:**
- "What do I have on SEC enforcement?" → Items matching topic
- "Show me all fintech articles" → Items matching sub-field
- "Anything about NVIDIA?" → Keyword search across titles + content

**By status:**
- "What haven't I read yet?" → `read: false` items
- "Show me items I've discussed" → `discussed: true` items
- "What was auto-fetched?" → `auto_fetched: true` items

**Combined:**
- "Unread fintech news from this week" → Combines date + topic + status filters

### 5.2 Summarization Display Format

#### Chat View (organized by date, then by topic within each date)

```
📻 Briefing — Today (April 15, 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📂 Securities & Enforcement (2 items)

  1. ⭐ SEC Files Insider Trading Charges Against Former XYZ Exec
     Source: Reuters Legal | Relevance: 92%
     Summary: The SEC charged a former VP of XYZ Corp with insider 
     trading ahead of the company's acquisition announcement...
     [unread] [auto-fetched]

  2. New SPAC Disclosure Requirements Take Effect
     Source: SEC.gov | Relevance: 78%
     Summary: Enhanced disclosure rules for SPAC transactions 
     become effective today, requiring...
     [unread] [auto-fetched]

📂 Corporate / M&A (1 item)

  3. Tech Giant Acquires AI Startup for $2B
     Source: Bloomberg Law | Relevance: 71%
     Summary: Major acquisition in the AI space with potential 
     antitrust implications...
     [unread] [auto-fetched]

📂 Your Saved Items (1 item)

  4. Client Memo - Johnson Case Filing
     Source: PDF upload | Saved manually
     Summary: Motion to dismiss filing with key arguments on 
     statute of limitations...
     [read] [discussed]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 This Week: 12 items total | 8 unread | 3 discussed
    Top topics: SEC enforcement (4), M&A (3), SPAC (2)
```

#### Voice View (same data, spoken format)

```
Agent: "Here's your briefing for today. You have 4 items across 
        3 topics.

        In Securities and Enforcement, two items. 
        
        First, and most important: The SEC filed insider trading 
        charges against a former executive at XYZ Corp. This is 
        related to trades made before the company's acquisition 
        was announced. Relevance score: ninety-two percent.
        
        Want to hear more about this one, or should I continue?"
```

### 5.3 Summarization Storage Structure

The index supports two organizational views:

**By Date (primary):**
```
<workspace>/
├── index.json                    # Master index with all items
├── articles/
│   ├── 2026-04-15_a1b2c3d4.md   # Today
│   ├── 2026-04-15_e5f6g7h8.md   # Today
│   ├── 2026-04-14_i9j0k1l2.md   # Yesterday
│   └── 2026-04-10_m3n4o5p6.md   # Last week
```

**By Topic (derived from index.json at query time):**

The `scripts/rank_and_summarize.py` script groups items by topic when generating output:

```
Input: rank_and_summarize.py --workspace-dir ... --timeframe today --group-by topic
Output:
{
  "timeframe": "today",
  "date": "2026-04-15",
  "groups": [
    {
      "topic": "Securities & Enforcement",
      "items": [
        {"rank": 1, "id": "a1b2c3d4", "title": "...", "score": 0.92, ...},
        {"rank": 2, "id": "e5f6g7h8", "title": "...", "score": 0.78, ...}
      ]
    },
    {
      "topic": "Corporate / M&A",
      "items": [
        {"rank": 3, "id": "i9j0k1l2", "title": "...", "score": 0.71, ...}
      ]
    }
  ],
  "stats": {
    "total": 4,
    "unread": 3,
    "discussed": 1,
    "top_topics": ["SEC enforcement", "M&A", "SPAC"]
  }
}
```

```
Input: rank_and_summarize.py --workspace-dir ... --timeframe week --group-by date
Output:
{
  "timeframe": "week",
  "groups": [
    {
      "date": "2026-04-15",
      "label": "Today",
      "items": [...]
    },
    {
      "date": "2026-04-14",
      "label": "Yesterday",
      "items": [...]
    },
    {
      "date": "2026-04-13",
      "label": "Sunday",
      "items": [...]
    }
  ]
}
```

### 5.4 Updated rank_and_summarize.py Interface

```
Usage: rank_and_summarize.py [options]

Options:
  --workspace-dir PATH   Agent workspace directory (required)
  --timeframe FRAME      One of: today, yesterday, week, month, all (default: all)
  --group-by GROUP       One of: topic, date, none (default: topic)
  --filter-topic TOPIC   Filter to specific topic (optional)
  --filter-status STATUS Filter: unread, read, discussed, all (default: all)
  --top-n INT            Return only top N items (default: all)
  --format FORMAT        One of: json, markdown (default: json)
```

---

## 6. Knowledge Base Location & Content Ingestion UX

### 6.1 Where the Knowledge Base Lives

The knowledge base lives in the **agent workspace**, which both the agent and the user can access:

```
~/.qwenpaw/workspaces/copilot-digest/
├── config.json              # User profile & interests
├── index.json               # Master content index
├── inbox/                   # Drop zone for files (user can drop files here directly)
├── articles/                # Extracted & processed content (managed by agent)
│   ├── 2026-04-15_a1b2c3d4.md
│   └── ...
├── work/                    # Work outputs (summaries, briefs, notes)
│   └── ...
└── exports/                 # Exported briefing documents
    └── ...
```

**Access model:**
- **Agent** reads/writes all directories via built-in file tools
- **User** can directly browse `~/.qwenpaw/workspaces/copilot-digest/` in Finder/Explorer to view articles, exports, and work outputs
- **User** can drop files into `inbox/` for the agent to auto-ingest (see 6.3)
- **Exports** in `exports/` are polished documents the user can open directly or share

### 6.2 How Users Send Files to the Agent

There are **4 ways** to get content into the briefer, ordered from easiest to most manual:

#### Method 1: Chat Attachment (Easiest - Any Channel)

Just send the file in the chat conversation, like sending a photo to a friend.

| Channel | How | What Happens |
|---------|-----|-------------|
| **Console (Web UI)** | Drag & drop file into chat, or click attachment button | File uploaded via `/console/upload` (max 10MB), agent receives `FileContent` |
| **Telegram** | Send document/file to the bot | Bot downloads via Telegram API, stores in `media/telegram/` |
| **Discord** | Drag & drop or attach file in Discord channel | Attachment URL extracted, content downloaded |
| **DingTalk** | Send file in DingTalk chat | Downloaded via DingTalk media API |
| **WeChat** | Send file in WeChat | Downloaded via WeChat media API |
| **iMessage** | Send file attachment | Received as file attachment |

The agent detects the file type (PDF, CSV, etc.), extracts content, and adds to the knowledge base automatically.

**Example conversation:**
```
User: [drops quarterly_report.pdf into chat]
User: "Save this for my reading list"

Agent: "Got it. I've added 'Q1 2026 Quarterly Report - XYZ Corp' to your 
        reading list under Finance > Earnings. 
        47 pages, ~12,000 words. Want a quick summary now or save it for later?"
```

#### Method 2: Paste a URL (Any Channel)

Just paste a URL into the chat. The agent fetches, extracts, and indexes the content.

```
User: "Save this: https://www.reuters.com/legal/sec-charges-xyz-insider-trading"

Agent: "Saved. 'SEC Charges XYZ Corp Executive with Insider Trading' 
        added to Securities & Enforcement. Relevance: 94%."
```

**Multiple URLs at once:**
```
User: "Add these to my reading list:
       https://example.com/article1
       https://example.com/article2  
       https://example.com/article3"

Agent: "Added 3 items to your reading list:
        1. 'Article Title 1' → Securities (relevance: 87%)
        2. 'Article Title 2' → M&A (relevance: 72%)
        3. 'Article Title 3' → General (relevance: 45%)"
```

#### Method 3: Inbox Drop Folder (For Bulk / Offline)

For users who want to add many files at once without chatting, there's a **drop folder**:

```
~/.qwenpaw/workspaces/copilot-digest/inbox/
```

**How it works:**
1. User drops PDF, CSV, or text files into `inbox/`
2. A scheduled cron job (every 30 minutes) checks the inbox
3. Agent processes each file: extracts content → indexes → moves to `articles/`
4. Original files moved to `inbox/processed/` (not deleted, in case user needs them)

**Cron job for inbox monitoring:**
```bash
qwenpaw cron create \
  --agent-id <agent_id> \
  --type agent \
  --name "briefer_inbox_scan" \
  --cron "*/30 * * * *" \
  --channel <channel> \
  --target-user <user_id> \
  --target-session <session_id> \
  --text "Check inbox/ for new files. Process any new PDFs, CSVs, or text files: extract content, add to reading list, move originals to inbox/processed/."
```

**Use case:** User downloads 5 PDFs from a legal database, drops them all into the inbox folder, and they show up in the next briefing.

#### Method 4: Browser Bookmarklet (One-Click Web Save)

A JavaScript bookmarklet the user adds to their browser toolbar. One click saves the current page to the briefer.

**Setup:** The agent generates a bookmarklet during onboarding:

```javascript
javascript:void(fetch('http://localhost:PORT/api/v1/agents/AGENT_ID/messages',{
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({text:'Save this URL to my reading list: '+location.href})
}).then(()=>alert('Saved to Copilot Digest!')).catch(()=>alert('QwenPaw not running')))
```

**How it works:**
1. User is browsing and finds an interesting article
2. Clicks the "Save to Briefer" bookmarklet in their browser toolbar
3. Bookmarklet sends the URL to the QwenPaw API
4. Agent receives it, fetches the page, extracts content, adds to reading list
5. User sees a small alert: "Saved to Copilot Digest!"

**Limitations:**
- Requires QwenPaw to be running locally
- Only works in browsers that support bookmarklets
- Not a full browser extension (no icon badge, no popup UI)
- For remote QwenPaw, the URL needs to be adjusted

### 6.3 Content Ingestion Summary

```
┌─────────────────────────────────────────────────────────┐
│                   Content Ingestion                      │
│                                                          │
│  ① Chat Attachment    ② Paste URL    ③ Inbox Folder     │
│  (drag & drop)        (any channel)  (bulk offline)      │
│       │                    │              │               │
│       ▼                    ▼              ▼               │
│  Channel media dir    browser_use    Cron scan           │
│  → FileContent        → snapshot     → read_file         │
│       │                    │              │               │
│       └────────────┬───────┴──────────────┘               │
│                    ▼                                      │
│           extract_content.py                             │
│           (PDF/CSV/HTML → markdown)                      │
│                    │                                      │
│                    ▼                                      │
│           index_manager.py add                           │
│           (→ index.json + articles/)                     │
│                                                          │
│  ④ Bookmarklet (one-click from browser)                  │
│       │                                                  │
│       ▼                                                  │
│  QwenPaw API → agent message → ② same as paste URL      │
└─────────────────────────────────────────────────────────┘
```

---

## 7. User Personas and Workflows

### 7.1 Persona: Lawyer

**Setup:** Topics = "securities law, SEC enforcement, M&A regulation"; Sources = SEC EDGAR, Reuters Legal  
**Typical session (voice, driving to court):**
1. Calls the agent: "What's new today?"
2. Agent briefs: "3 new items. First: SEC filed an enforcement action against XYZ Corp for insider trading..."
3. Lawyer: "That's relevant to the Chen case. Draft a case brief comparing the facts."
4. Agent drafts a case brief, reads it back
5. Lawyer: "Add a note about the statute of limitations issue. And put 'review XYZ filing' on my action items."
6. Agent updates the brief and creates an action item
7. Later: lawyer exports the brief and action items from chat

### 7.2 Persona: Finance Analyst

**Setup:** Topics = "fintech, crypto regulation, IPO market"; Sources = Bloomberg, Financial Times  
**Typical session (chat, waiting for flight):**
1. "Give me this week's briefing, ranked by importance"
2. Agent shows ranked list with summaries
3. "Summarize #2 and #4 in executive summary format for my team"
4. Agent produces a polished executive summary
5. "Export that as a file"
6. Agent sends markdown via `send_file_to_user`

### 7.3 Persona: Reporter

**Setup:** Topics = "AI policy, tech antitrust, privacy regulation"; Sources = TechCrunch, The Verge, Ars Technica  
**Typical session (voice, at the gym):**
1. "Anything breaking today?"
2. Agent briefs on 2 new stories
3. "Organize my notes from yesterday's interview with the AI policy angle"
4. Agent structures notes, suggests angles
5. "Draft an intro paragraph for the story"
6. Agent drafts, reads it back, iterates based on feedback

---

## 8. Detailed Design

### 5.1 Skill File Structure

```
src/qwenpaw/agents/skills/copilot_digest/
├── SKILL.md                              # Main skill instructions
├── references/
│   └── ranking_criteria.md               # Scoring heuristics doc
└── scripts/
    ├── index_manager.py                  # Knowledge base CRUD
    ├── extract_content.py                # Content extraction pipeline
    ├── rank_and_summarize.py             # Scoring and ranking engine
    └── export_summary.py                 # Export document generator
```

### 5.2 SKILL.md Sections

#### Section 1: Trigger Conditions

The skill activates when the user:
- Sends a PDF, CSV, or other document file
- Shares a URL and asks to save/read/track it
- Asks for a "briefing", "what's new", "catch me up", "reading list"
- Wants to discuss a previously saved article
- Says "draft", "summarize", "organize notes", "action items", "write up"
- Configures interests, sources, or schedule
- Calls in via voice and expects a briefing

#### Section 2: First-Time Setup

Guided 5-step onboarding (see Section 4 for full flow):
1. **Major Field** — Select primary professional domain (Law, Finance, Media, Tech, Healthcare, Government, Academia)
2. **Sub-Fields** — Select 1-3 sub-fields within the major field (e.g., Law → Securities & Capital Markets, Corporate / M&A)
3. **Topics & Keywords** — Free-text list of specific topics to track (e.g., "SEC enforcement", "SPAC litigation")
4. **Sources** — Accept suggested defaults for the field, add/remove as needed
5. **Schedule & Preferences** — Fetch frequency, briefing length, language
6. Write `config.json` with full profile
7. Create workspace directory structure
8. Optionally set up the cron job for auto-enrichment

#### Section 3: Content Ingestion

Three input paths, all ending with `scripts/index_manager.py add`:

**PDF files:**
```
1. User sends PDF → agent detects file type
2. Extract text: execute_python_code with pdfplumber
3. Agent generates title + summary from extracted text
4. Save extracted content to articles/{date}_{id}.md
5. Call: scripts/index_manager.py add --title "..." --source-type pdf ...
```

**CSV files:**
```
1. User sends CSV → agent detects file type
2. Parse with pandas, identify key columns
3. Convert to structured markdown summary
4. Save to articles/{date}_{id}.md
5. Call: scripts/index_manager.py add --title "..." --source-type csv ...
```

**Web URLs:**
```
1. User pastes URL or says "save this page"
2. browser_use: open URL → snapshot → extract content
3. Agent generates title + summary
4. Save cleaned content to articles/{date}_{id}.md
5. Call: scripts/index_manager.py add --title "..." --source-type url ...
```

#### Section 4: Interest Configuration

The agent reads/writes `config.json`:
- User can say "add fintech to my interests" → agent updates config
- User can say "remove sports" → agent updates config
- User can say "add Reuters as a source" → agent adds to sources list
- User can say "change fetch schedule to every 4 hours" → agent updates cron expression
- Changes take effect on next auto-fetch cycle

#### Section 5: Auto-Enrichment (Cron Composition)

The SKILL.md provides a template for composing with the `cron` skill:

```bash
qwenpaw cron create \
  --agent-id <agent_id> \
  --type agent \
  --name "briefer_auto_fetch" \
  --cron "<from config.schedule.fetch_cron>" \
  --channel <channel> \
  --target-user <user_id> \
  --target-session <session_id> \
  --text "Auto-fetch: Check briefer config and fetch latest content for configured interests. Add new items to reading list."
```

When the cron fires:
1. Agent re-enters copilot_digest skill context
2. Reads `config.json` for topics, sources, keywords
3. For each configured source URL: `browser_use` → open → snapshot → extract
4. Filters content by keyword relevance
5. Adds matching items to `index.json` with `auto_fetched: true`
6. Silently updates - no notification unless 3+ new high-relevance items found

#### Section 6: Briefing & Ranking

When user asks for a briefing:
1. Call `scripts/rank_and_summarize.py --workspace-dir ... --timeframe <today|week|all>`
2. Script reads `index.json` + `config.json`, scores each item:
   - **Recency** (40%): exponential decay from save time
   - **Relevance** (40%): keyword/topic overlap with configured interests
   - **Source authority** (20%): known authoritative sources score higher
3. Returns ranked JSON to stdout
4. Agent presents as organized list, grouped by timeframe:
   - "Today (3 items)"
   - "This week (7 items)"
   - "Older (2 items)"
5. Each item shows: rank, title, source, one-line summary, topics

#### Section 7: Interactive Discussion

The agent loads article content from `articles/` and engages in freeform Q&A:
- User can reference articles by number, title, or topic
- Agent uses the full extracted content (not just summary) for deep discussion
- Cross-referencing between articles when relevant
- After discussion, marks items as `discussed: true`

#### Section 8: Hands-Free Work Mode

Detects work intent and produces role-appropriate output:

**Work types:**
| Command | Output | Storage |
|---------|--------|---------|
| "Draft a summary of X" | Structured summary | `work/summary_{date}_{id}.md` |
| "Prepare a case brief" | Legal brief format | `work/brief_{date}_{topic}.md` |
| "Organize my notes on X" | Structured outline | `work/notes_{date}_{topic}.md` |
| "Write action items" | Checklist | `work/action_items_{date}.md` |
| "Draft an email about X" | Email draft | `work/email_{date}_{topic}.md` |
| "Compare articles X and Y" | Comparison analysis | `work/comparison_{date}_{id}.md` |

**Role-specific formatting:**
- **Lawyer**: Issue → Rule → Application → Conclusion (IRAC); citations; case comparisons
- **Finance**: Executive summary; key metrics; risk/opportunity matrix; market implications
- **Reporter**: Inverted pyramid; key quotes; source attribution; angle suggestions
- **General**: Clean bullet points; key takeaways; next steps

**Voice workflow**: Agent reads back drafts, takes verbal corrections, iterates until user approves.

#### Section 9: Export

When user asks to export:
1. Call `scripts/export_summary.py` with selected item IDs and/or work files
2. Script compiles into a clean markdown document with:
   - Header with date and topic
   - Table of contents
   - Each selected article summary
   - Any work outputs (briefs, notes, action items)
3. Deliver via `send_file_to_user`

---

### 5.3 Knowledge Base Schema

#### index.json

```json
{
  "version": 1,
  "items": [
    {
      "id": "string (8-char hex)",
      "title": "string",
      "source_type": "url | pdf | csv",
      "source_url": "string (optional)",
      "source_file": "string (optional, original filename)",
      "saved_at": "ISO 8601 datetime",
      "topics": ["string"],
      "summary": "string (1-paragraph)",
      "content_path": "string (relative path to articles/)",
      "word_count": "integer",
      "read": "boolean (default false)",
      "discussed": "boolean (default false)",
      "relevance_score": "float (0.0-1.0, computed by ranking script)",
      "auto_fetched": "boolean (default false)"
    }
  ]
}
```

#### config.json

```json
{
  "profile": {
    "major_field": "string (law | finance | media | technology | healthcare | government | academia)",
    "sub_fields": ["string (field-specific sub-categories, see Section 4.1)"],
    "topics": ["string (specific topics/keywords to track)"],
    "role_title": "string (free-text, e.g. 'Securities lawyer')"
  },
  "sources": [
    {
      "name": "string",
      "url": "string",
      "enabled": "boolean (default true)"
    }
  ],
  "schedule": {
    "fetch_cron": "string (cron expression, default '0 */6 * * *')",
    "max_items_per_fetch": "integer (default 10)"
  },
  "preferences": {
    "language": "string (default 'en')",
    "summary_length": "brief | standard | detailed (default 'standard')",
    "briefing_style": "string (default 'professional')"
  }
}
```

---

### 5.4 Script Specifications

#### index_manager.py

**Purpose:** Deterministic CRUD operations on the content index. The LLM should not manually edit `index.json` - all mutations go through this script.

**Interface:**
```
Usage: index_manager.py <command> [options]

Commands:
  add           Add a new item to the index
  list          List items with optional filters
  mark-read     Mark an item as read
  mark-discussed  Mark an item as discussed
  remove        Remove an item from the index
  stats         Show statistics about the knowledge base

Global options:
  --workspace-dir PATH   Agent workspace directory (required)

add options:
  --title TEXT           Article title (required)
  --source-type TYPE     One of: url, pdf, csv (required)
  --source-url URL       Source URL (for url type)
  --source-file NAME     Original filename (for file types)
  --topics TOPICS        Comma-separated topic list
  --summary TEXT         One-paragraph summary
  --content-path PATH    Relative path to content file (required)
  --word-count INT       Word count of content
  --auto-fetched         Flag if auto-fetched by cron

list options:
  --filter FILTER        One of: today, week, unread, discussed, all (default: all)
  --format FORMAT        One of: json, table (default: table)
  --limit INT            Max items to return (default: 50)

Output: JSON to stdout for programmatic use, table for human display.
```

**Implementation notes:**
- Uses atomic file writes (write to temp file, then rename) for index.json safety
- Generates 8-character hex IDs via `secrets.token_hex(4)`
- All timestamps in UTC ISO 8601

#### extract_content.py

**Purpose:** Extract clean readable content from various source types into markdown files.

**Interface:**
```
Usage: extract_content.py [options]

Options:
  --url URL              Fetch and extract web content
  --file PATH            Extract from local file (PDF or CSV)
  --output PATH          Output markdown file path (required)
  --max-length INT       Max content length in characters (default: 50000)
```

**Implementation notes:**
- Web extraction: `requests` + `BeautifulSoup` with `html.parser`. Strips nav, ads, sidebars. Falls back to full body text.
- PDF extraction: `pdfplumber` for text, falls back to `pypdf` if pdfplumber unavailable
- CSV extraction: `pandas` or stdlib `csv`. Produces a markdown table with first 100 rows + summary stats
- Output is clean markdown with title, source attribution, and extracted date

#### rank_and_summarize.py

**Purpose:** Score and rank all items in the index based on configured interests and recency.

**Interface:**
```
Usage: rank_and_summarize.py [options]

Options:
  --workspace-dir PATH   Agent workspace directory (required)
  --timeframe FRAME      One of: today, week, all (default: all)
  --top-n INT            Return only top N items (default: all)
  --format FORMAT        One of: json, markdown (default: json)
```

**Scoring algorithm:**
```
score = (recency_weight * recency_score) 
      + (relevance_weight * relevance_score)
      + (authority_weight * authority_score)

Where:
  recency_score = exp(-decay_rate * hours_since_saved)
  relevance_score = jaccard(item_topics + item_keywords, config_topics + config_keywords)
  authority_score = 1.0 if source in known_authoritative_sources else 0.5
  
Default weights: recency=0.4, relevance=0.4, authority=0.2
```

**Output (JSON mode):**
```json
{
  "timeframe": "today",
  "total_items": 5,
  "ranked_items": [
    {
      "rank": 1,
      "id": "abc123",
      "title": "...",
      "score": 0.87,
      "score_breakdown": {"recency": 0.95, "relevance": 0.80, "authority": 1.0},
      "summary": "...",
      "topics": ["..."],
      "saved_at": "...",
      "read": false
    }
  ]
}
```

#### export_summary.py

**Purpose:** Compile selected articles and work outputs into a polished, exportable document.

**Interface:**
```
Usage: export_summary.py [options]

Options:
  --workspace-dir PATH     Agent workspace directory (required)
  --item-ids IDS           Comma-separated article IDs to include
  --work-files FILES       Comma-separated work file names to include
  --include-all-work       Include all work files from today
  --format FORMAT          One of: md, txt (default: md)
  --output PATH            Output file path (required)
  --title TEXT             Document title (default: "Briefing - {date}")
```

**Output format (markdown):**
```markdown
# Briefing - April 15, 2026

## Table of Contents
1. Article Summaries
2. Work Outputs
3. Action Items

---

## 1. Article Summaries

### SEC Enforcement Action Against XYZ Corp
**Source:** Reuters Legal | **Saved:** Apr 15, 2026 | **Topics:** securities law, enforcement
[Full summary...]

### ...

## 2. Work Outputs

### Case Brief: XYZ Corp Comparison
[Content from work/brief_...]

## 3. Action Items
- [ ] Review XYZ filing
- [ ] ...
```

---

## 9. Integration Points

### 6.1 Cron Skill (Auto-Enrichment)

The copilot_digest SKILL.md instructs the agent to create a cron job during setup:

```bash
qwenpaw cron create \
  --agent-id <agent_id> \
  --type agent \
  --name "briefer_auto_fetch" \
  --cron "0 */6 * * *" \
  --channel <user_channel> \
  --target-user <user_id> \
  --target-session <session_id> \
  --text "Auto-fetch: Check briefer config and fetch latest content for configured interests. Add new items to reading list. Only notify if 3+ new high-relevance items found."
```

### 6.2 Voice Channel (Twilio)

No code changes needed. The skill provides voice-aware instructions:
- The agent already receives voice input as transcribed text via STT
- The agent already outputs text that gets spoken via TTS
- The skill simply instructs the agent to **format differently** for voice delivery

Relevant existing infrastructure:
- `src/qwenpaw/app/channels/voice/channel.py` - Channel implementation
- `src/qwenpaw/app/channels/voice/conversation_relay.py` - WebSocket for real-time streaming
- `src/qwenpaw/config/config.py` - `VoiceChannelConfig` with TTS/STT settings

### 6.3 PDF Skill (Document Extraction)

The copilot_digest can reference the `pdf` skill's extraction patterns for complex PDFs. For simple text extraction, the skill's own `extract_content.py` script handles it directly.

### 6.4 Skill Config System

User interests are stored in `config.json` (workspace file). Additionally, they can be injected via the skill manifest config mechanism:
- `skills_manager.py` line ~590: `_build_skill_config_env_overrides()` maps config keys to env vars
- The `BRIEFER_INTERESTS` env var declared in frontmatter will be auto-injected if set in skill.json

---

## 10. Limitations and Future Considerations

### Current Limitations

| Limitation | Impact | Potential Future Solution |
|-----------|--------|--------------------------|
| No Chrome extension for one-click save | User must paste URLs into chat | Build a bookmarklet or browser extension that POSTs to QwenPaw API |
| Paywall content | Cannot extract full text from paywalled sources | User provides PDF/screenshot of paywalled content manually |
| Heuristic ranking only | Scoring is keyword-based, not semantic | Could add embedding-based similarity with sentence-transformers |
| No push notifications | Agent doesn't proactively notify about breaking news | Cron can approximate this by checking frequently and sending via channel |

### Future Enhancements (Not in v1)

- **Bookmarklet**: A `javascript:void(...)` snippet users add to their browser toolbar that sends the current page URL to the QwenPaw API
- **Embedding-based search**: Use sentence-transformers to enable semantic search across saved articles
- **Multi-agent collaboration**: A dedicated "researcher" agent that deeply investigates topics on behalf of the briefer
- **Calendar integration**: Auto-schedule briefings based on the user's calendar gaps

---

## 11. Implementation Checklist

- [ ] Create directory: `src/qwenpaw/agents/skills/copilot_digest/`
- [ ] Write `SKILL.md` (~550 lines, all 10 sections)
- [ ] Write `scripts/index_manager.py` (~200 lines)
- [ ] Write `scripts/extract_content.py` (~150 lines)
- [ ] Write `scripts/rank_and_summarize.py` (~120 lines)
- [ ] Write `scripts/export_summary.py` (~100 lines)
- [ ] Write `references/ranking_criteria.md` (~50 lines)
- [ ] Verify skill loads: `qwenpaw skills list`
- [ ] Test content ingestion (PDF, URL)
- [ ] Test config setup flow
- [ ] Test briefing with ranking
- [ ] Test work mode (draft summary)
- [ ] Test export
- [ ] Test cron auto-fetch setup
