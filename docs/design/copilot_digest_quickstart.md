# Copilot Digest — Quick Start Guide

A step-by-step guide to try the Copilot Digest skill on your QwenPaw agent.

---

## What Is Copilot Digest?

It turns your QwenPaw agent into a personal content copilot. You feed it articles, PDFs, and URLs — it organizes everything, gives you ranked briefings, discusses topics with you, and helps you draft summaries and notes. Works in chat or over a voice call, so you can use it hands-free while commuting, exercising, or waiting.

---

## Prerequisites

- QwenPaw installed and initialized (`qwenpaw init` done)
- At least one LLM provider configured (e.g., Claude, GPT)
- Python packages for content extraction (optional but recommended):
  ```bash
  pip install pdfplumber beautifulsoup4 requests
  ```

---

## Step 1: Create a Dedicated Agent (Recommended)

We recommend creating a dedicated agent for the briefer so it has its own workspace and doesn't interfere with your default agent.

1. Open the QwenPaw Console
2. Go to **Agents** and click **Create Agent**
3. Give it a name like "Copilot Digest"
4. QwenPaw will suggest a workspace path (e.g., `~/.qwenpaw/workspaces/copilot-digest`). Accept it or customize as you like.

Alternatively, you can skip this step and use your existing `default` agent.

---

## Step 2: Install the Skill

You need to know two things first:

- **Your QwenPaw home directory:** Legacy installs use `~/.copaw`, new installs use `~/.qwenpaw`. Check which one exists on your machine.
- **Your agent's workspace path:** Find it in the Console under your agent's settings, or run `qwenpaw agents list`.

### Copy directly to the agent's workspace

If you know your agent's workspace path, copy the skill there directly.

```bash
# Set your QwenPaw home and agent workspace name
# QWENPAW_HOME=~/.qwenpaw
# WORKSPACE=copilot-digest    # or "default" for the default agent

mkdir -p $QWENPAW_HOME/workspaces/$WORKSPACE/skills/copilot_digest
cp -r src/qwenpaw/agents/skills/copilot_digest/* ~/.qwenpaw/workspaces/copilot-digest/skills/copilot_digest/
```

### Copy to the skill pool (recommended for multi-agent setups)

If you want the skill available to **all agents**, copy it to the shared skill pool instead. Any agent can then enable it from the Console.

```bash
QWENPAW_HOME=~/.qwenpaw

mkdir -p $QWENPAW_HOME/skill_pool/copilot_digest
cp -r src/qwenpaw/agents/skills/copilot_digest/* \
  $QWENPAW_HOME/skill_pool/copilot_digest/
```

Then go to the Console → **Skills** page and broadcast (or manually enable) the skill for the agents that need it.

### Verify the install

After copying, the folder should contain:

```
skills/copilot_digest/
├── SKILL.md
├── scripts/
│   ├── index_manager.py
│   ├── extract_content.py
│   ├── rank_and_summarize.py
│   └── export_summary.py
└── references/
    └── ranking_criteria.md
```

No reinstall needed — just restart QwenPaw (`qwenpaw app`) and the skill will be picked up automatically.

---

## Step 3: Enable the Skill

After installing and restarting, enable the skill for your agent.

---

## Step 4: Set Up Your Profile

Start a conversation with the agent. The first time you use the skill, it walks you through a guided setup — just reply with numbers or short answers:

```
You:   "Set up my briefer"

Agent: "Welcome to Copilot Digest! Let's set up your profile.

        What's your primary professional field?

          1. Law
          2. Finance & Banking
          3. Media & Journalism
          4. Technology
          5. Healthcare & Pharma
          6. Government & Policy
          7. Academia & Research
          8. Other

        Reply with a number (1-8):"

You:   "2"

Agent: "Finance & Banking — got it!

        Pick 1-3 sub-fields (e.g. '1, 4'):

          1. Investment Banking
          2. Asset Management
          3. PE / VC
          4. Fintech
          5. Risk & Compliance
          6. Crypto & Digital Assets
          7. Insurance
          8. Macro & Economics"

You:   "4, 6"

Agent: "Fintech + Crypto — great combo.

        Here are suggested topics. Pick any that apply, 
        and feel free to add your own:

          1. DeFi regulation
          2. Stablecoin policy
          3. CBDC
          4. Crypto exchange compliance
          5. Open banking / PSD2
          6. Digital payments
          7. Blockchain infrastructure
          8. NFT / tokenization

          + Type any additional topics"

You:   "1, 2, 3"

Agent: "Here are recommended sources for Finance + Fintech/Crypto:

          ✓  1. Bloomberg
          ✓  2. Financial Times
          ✓  3. Reuters
          ✓  4. Wall Street Journal
          ✓  5. CoinDesk
          ✓  6. The Block
          ✓  7. CNBC
          ✓  8. Federal Reserve

        Want to change any?
          • Remove by number (e.g. 'remove 2, 7')
          • Add a source (e.g. 'add Decrypt')
          • Or reply 'looks good'"

You:   "add Decrypt, remove 2"

Agent: "How often should I check for new content?

          A. Every 2 hours
          B. Every 4 hours
          C. Every 6 hours (recommended)
          D. Twice daily
          E. Once daily"

You:   "B"

Agent: "How detailed should your briefings be?

          A. Brief — headlines + one-line summary
          B. Standard — paragraph summaries (recommended)
          C. Detailed — full analysis with context"

You:   "B"

Agent: "Here's your profile:

          Field:      Finance & Banking
          Sub-fields: Fintech, Crypto & Digital Assets
          Topics:     DeFi regulation, stablecoin policy, CBDC
          Sources:    Bloomberg, Reuters, WSJ, CoinDesk, 
                      The Block, Decrypt, CNBC, Federal Reserve
          Schedule:   Every 4 hours
          Briefings:  Standard

        All good? (yes / change anything)"

You:   "yes"

Agent: "All set! Your profile is saved.
        Want me to set up automatic news fetching now?"

You:   "Yes"

Agent: [creates a cron job to auto-fetch every 4 hours]
```

Your profile is saved at `<workspace>/config.json`.

---

## Step 5: Add Content

There are four ways to feed content to your briefer:

### Drop a File in Chat

Just drag and drop a PDF or CSV into the chat window (or send it as an attachment in Telegram, Discord, etc.):

```
You:   [drops quarterly_report.pdf]
You:   "Save this"

Agent: "Added: 'Q1 2026 Quarterly Report — Acme Corp'
        → Fintech | 47 pages | Relevance: 82%"
```

### Paste a URL

```
You:   "Save this: https://www.coindesk.com/stablecoin-regulation-update"

Agent: "Saved: 'EU Stablecoin Regulation Takes Effect'
        → Crypto, Regulation | Relevance: 91%"
```

### Drop Files in the Inbox Folder

For bulk adding — drop files directly into the folder:

```
<workspace>/inbox/
```

The agent scans this folder every 30 minutes and processes any new files automatically. Originals are moved to `inbox/processed/` after extraction.

### Browser Bookmarklet (One-Click Save)

Ask the agent to set up a bookmarklet:

```
You:   "Give me a bookmarklet to save pages from my browser"

Agent: [generates a JavaScript bookmark you add to your browser toolbar]
```

Then while browsing, just click the bookmarklet to save the current page.

---

## Step 6: Get a Briefing

### In Chat

```
You:   "What's new today?"

Agent: "📻 Briefing — Today (April 15, 2026)

        📂 Crypto & Regulation (2 items)
        
        1. ⭐ EU Stablecoin Regulation Takes Effect
           Source: coindesk.com | Relevance: 91%
           New MiCA rules for stablecoin issuers become effective...
           [unread]
        
        2. Fed Publishes CBDC Research Paper
           Source: federalreserve.gov | Relevance: 85%
           The Federal Reserve released findings on...
           [unread]
        
        📂 DeFi (1 item)
        
        3. Major DeFi Protocol Passes $10B TVL
           Source: theblock.co | Relevance: 68%
           ...
        
        📊 3 items | 3 unread | 0 discussed
        
        Want to discuss any of these?"
```

### Over a Voice Call (Twilio)

If you have the voice channel set up, just call your agent's phone number:

```
Agent: "Good morning. You have 3 items in today's briefing.
        Starting with the most important.
        
        First: The EU stablecoin regulation under MiCA just 
        took effect. This sets new requirements for stablecoin 
        issuers operating in Europe. Relevance: ninety-one percent.
        
        Would you like to discuss this, or should I continue?"

You:   "Tell me more about the impact on DeFi"

Agent: [discusses in detail]

You:   "Draft a note about the key implications for our portfolio"

Agent: [drafts note, reads it back]
```

---

## Step 7: Discuss and Work

### Discuss an Article

```
You:   "Tell me more about #1"

Agent: [loads the full article and engages in Q&A]

You:   "How does this compare to what the US is doing?"

Agent: [cross-references with other saved articles if available]
```

### Draft Work Output

```
You:   "Write an executive summary of items 1 and 2 for my team"

Agent: [produces a structured summary tailored to your field]
       "Here's the executive summary:
        
        Key Findings:
        1. EU MiCA stablecoin rules are now live...
        2. Fed CBDC paper suggests cautious approach...
        
        Impact: ...
        
        Saved to: work/summary_2026-04-15_crypto_regulation.md
        Want me to export this?"
```

### Other Work You Can Ask For

- "Organize my notes on stablecoin regulation"
- "Write action items from today's briefing"
- "Draft an email about the CBDC paper to my team"
- "Compare articles 1 and 3"
- "Prepare a case brief on the MiCA implications" (if you're a lawyer)

---

## Step 8: Export

```
You:   "Export today's briefing"

Agent: [compiles all read/discussed articles + any work outputs into a 
        polished markdown document and sends it to you as a file]
       
       "Here's your export: briefing_2026-04-15.md
        Includes: 2 article summaries, 1 executive summary, 
        and your action items."
```

You can also find all exports in: `<workspace>/exports/`

---

## Useful Commands

| What You Say | What Happens |
|-------------|-------------|
| "What's new today?" | Today's ranked briefing |
| "This week's briefing" | Past 7 days, ranked |
| "Unread items on crypto" | Filtered by topic + status |
| "Save this: [URL]" | Add a webpage to your list |
| "Add DeFi to my topics" | Update your interests |
| "What sources am I tracking?" | Show your configured sources |
| "How many items do I have?" | Show stats |
| "Draft a summary of #2" | Produce work output |
| "Export today's briefing" | Get a polished document |
| "Set up auto-fetch" | Create the cron job |

---

## Where Things Are Stored

Everything lives in your agent's workspace:

```
<workspace>/
├── config.json       ← Your profile and interests (editable)
├── index.json        ← Content index (managed by the agent)
├── inbox/            ← Drop files here for auto-ingestion
├── articles/         ← Extracted article content
├── work/             ← Your summaries, notes, briefs
└── exports/          ← Exported briefing documents
```

You can browse these folders directly to read articles or grab exports.

---

## Updating Your Interests

Your profile is not fixed. Change it anytime:

```
You:   "Remove Crypto from my sub-fields, add Risk & Compliance"
You:   "Add SEC enforcement to my topics"
You:   "Add Law360 as a source"
You:   "Change my fetch schedule to every 2 hours"
You:   "Switch to detailed briefings"
```

---

## Troubleshooting

**Skill not showing up after copying files**
→ Make sure you copied to the right directory. Your QwenPaw home could be `~/.copaw` (legacy) or `~/.qwenpaw` (new). Check which one has your `workspaces/` folder. Also try adding to the **skill pool** (`$QWENPAW_HOME/skill_pool/copilot_digest/`) and broadcasting from the Console.

**"Skill not found" when chatting**
→ Make sure you enabled it (Step 3). Go to the Skills page in the Console and check the toggle.

**"No items found"**
→ You need to add content first (Step 5) or set up auto-fetch (Step 4).

**PDF extraction fails**
→ Install pdfplumber: `pip install pdfplumber`

**URL extraction gives poor results**
→ Install readability-lxml for better extraction: `pip install readability-lxml`

**Auto-fetch not working**
→ Check if the cron job exists: `qwenpaw cron list --agent-id <your_agent_id>`

