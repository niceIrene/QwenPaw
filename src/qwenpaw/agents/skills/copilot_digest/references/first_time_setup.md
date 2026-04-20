# First-Time Setup

If `config.json` does not exist in the workspace, run this onboarding flow. Present each step as a **multiple-choice question** so the user can reply with just a number or letter. Keep it conversational — one question per message, confirm each answer before moving on.

## Step 1 — Major Field

Present as a numbered list. The user replies with a number:

```
Welcome to Copilot Digest! Let's set up your profile.

What's your primary professional field?

  1. Law
  2. Finance & Banking
  3. Media & Journalism
  4. Technology
  5. Healthcare & Pharma
  6. Government & Policy
  7. Academia & Research
  8. Other (tell me your field)

Reply with a number (1-8):
```

## Step 2 — Sub-Fields

Based on the selected field, present the relevant sub-fields as a multi-select. The user picks 1–3 by number:

**Law:**
```
Pick 1-3 sub-fields (e.g. "1, 3, 5"):

  1. Corporate & M&A
  2. Securities & Capital Markets
  3. Litigation & Dispute Resolution
  4. Intellectual Property
  5. Employment & Labor
  6. Regulatory & Compliance
  7. Criminal & White Collar
  8. International Trade
```

**Finance & Banking:**
```
  1. Investment Banking
  2. Asset Management
  3. PE / VC
  4. Fintech
  5. Risk & Compliance
  6. Crypto & Digital Assets
  7. Insurance
  8. Macro & Economics
```

**Media & Journalism:**
```
  1. Tech
  2. Politics
  3. Business
  4. Science & Health
  5. Culture & Society
  6. Investigative
  7. International
```

**Technology:**
```
  1. AI / ML
  2. Cloud & Infrastructure
  3. Cybersecurity
  4. Product Management
  5. Engineering & DevTools
  6. Data Science
  7. Web3 & Blockchain
```

**Healthcare & Pharma:**
```
  1. Drug Development
  2. Medical Devices
  3. Health Policy
  4. Digital Health
  5. Biotech & Genomics
```

**Government & Policy:**
```
  1. Domestic Policy
  2. Foreign Affairs
  3. Trade & Commerce
  4. Defense & Intelligence
  5. Environment & Energy
```

**Academia & Research:**
```
  1. Computer Science
  2. Economics
  3. Social Sciences
  4. Natural Sciences
  5. Humanities
```

## Step 3 — Topics & Keywords

Suggest topics based on the chosen sub-fields as a multi-select with an open-ended option. Examples for Finance + Fintech/Crypto:

```
Here are suggested topics for your sub-fields. Pick any that apply (e.g. "1, 3, 5"), and feel free to add your own:

  1. DeFi regulation
  2. Stablecoin policy
  3. CBDC (central bank digital currency)
  4. Crypto exchange compliance
  5. Open banking / PSD2
  6. Digital payments
  7. Blockchain infrastructure
  8. NFT / tokenization
  
  +  Type any additional topics you'd like to track
```

If the user's field/sub-field combination has obvious hot topics, suggest those. Always allow free-text additions.

## Step 4 — Preferred Sources

Present the default sources for their field as a pre-selected checklist. The user can add or remove:

```
Here are recommended sources for Finance + Fintech/Crypto:

  ✓  1. Bloomberg
  ✓  2. Financial Times
  ✓  3. Reuters
  ✓  4. Wall Street Journal
  ✓  5. CoinDesk
  ✓  6. The Block
  ✓  7. CNBC
  ✓  8. Federal Reserve

Want to change any?
  • Remove by number (e.g. "remove 2, 7")
  • Add a source (e.g. "add Decrypt")
  • Or reply "looks good" to accept
```

Default source lists per field:

| Field | Default Sources |
|-------|----------------|
| Law | Reuters Legal, Law360, SEC EDGAR, SCOTUS Blog, National Law Review, Bloomberg Law |
| Finance | Bloomberg, Financial Times, Reuters, WSJ, CNBC, Seeking Alpha, Federal Reserve |
| Media | AP News, Reuters, Nieman Lab, CJR, Poynter, Press Gazette |
| Technology | TechCrunch, The Verge, Ars Technica, Hacker News, MIT Tech Review, Wired |
| Healthcare | STAT News, BioPharma Dive, FDA.gov, NEJM, Nature Medicine |
| Government | Federal Register, Congressional Record, GAO Reports, Politico, The Hill |
| Academia | Google Scholar, arXiv, SSRN, Nature, Science |

## Step 5 — Schedule & Preferences

Present as two quick multiple-choice questions:

```
How often should I check for new content?

  A. Every 2 hours
  B. Every 4 hours
  C. Every 6 hours (recommended)
  D. Twice daily (morning & evening)
  E. Once daily
```

Then:

```
How detailed should your briefings be?

  A. Brief — headlines + one-line summary
  B. Standard — paragraph summaries (recommended)
  C. Detailed — full analysis with context
```

## Confirmation

After all steps, show a summary and ask for confirmation before saving:

```
Here's your profile:

  Field:      Finance & Banking
  Sub-fields: Fintech, Crypto & Digital Assets
  Topics:     DeFi regulation, stablecoin policy, CBDC
  Sources:    Bloomberg, Reuters, WSJ, CoinDesk, The Block, Decrypt
  Schedule:   Every 4 hours
  Briefings:  Standard

All good? (yes / change anything)
```

## Save Config

Write the config to `config.json`:

```json
{
  "profile": {
    "major_field": "<selected>",
    "sub_fields": ["<selected>", "..."],
    "topics": ["<user input>", "..."],
    "role_title": "<user's description of their role>"
  },
  "sources": [
    {"name": "<source name>", "url": "<source url>", "enabled": true}
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

Create the workspace directory structure:
```bash
mkdir -p articles work exports inbox inbox/processed
```

Then offer to set up auto-enrichment (see Section 5 in SKILL.md).
