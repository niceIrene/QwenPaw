# Summarization Guide

Rules for generating the podcast-style summary script during content ingestion.

**This step is mandatory for every ingested item.** The summary is what gets shown in briefings and included in exports. Without it, the item is effectively invisible.

---

## What to produce

A **podcast-style script** — the kind of thing a knowledgeable host would say to walk a listener through the article. It is NOT a headline, NOT a bullet list, NOT a copy-paste of the original.

## Length

**400-800 words** (roughly 2-4 minutes read-aloud).

## Style (auto-match to article type)

| Article type | Tone |
|---|---|
| Lifestyle / opinion / culture | Casual, conversational |
| Science / business / tech | Clear explainer, knowledgeable host |
| News / policy / current events | News-analysis style |
| Academic paper / research | Accessible explainer, connect findings to real-world impact |

## Writing rules

1. **Open with a hook** — a scene, a question, or a surprising fact. Never open by reading the title.
2. **Speak, don't write.** Short sentences. Contractions are fine. Explain jargon in passing ("the Jaccard index — basically a way to measure how much two sets overlap").
3. **Structure: what happened -> why -> why it matters to the listener.**
4. **Add context and analogies.** Don't just restate the article. Connect it to things the listener already knows.
5. **End with a takeaway or a thought to sit with.** Not a generic "time will tell" — something specific.

## For academic papers specifically

- Lead with the research question in plain language.
- Explain the method in one sentence (skip the math unless it's the point).
- State the key finding clearly.
- Explain why this matters beyond the field.
- Mention limitations if they're significant.

## Output format

Write the summary as plain markdown text. No frontmatter, no metadata headers — just the script content. It will be saved to `articles/{date}_{id}_script.md` and passed to the indexer via `--summary-file`.

## Example

Given an article about a new SEC enforcement action:

> Imagine you're a VP at a mid-cap tech company, and you just heard your firm is about to be acquired. What do you do with that information? Well, according to the SEC, one executive at XYZ Corp decided to buy $2 million in call options the week before the deal went public.
>
> The SEC filed charges yesterday against James Chen, former VP of corporate strategy at XYZ Corp, alleging a textbook case of insider trading. Chen allegedly used material nonpublic information about a pending $4.2 billion acquisition to make trades that netted roughly $800,000 in profit.
>
> What makes this case notable isn't the amount — it's the detection method. The SEC flagged the trades using a new pattern-matching system that cross-references options activity with M&A timelines. This is the third case in six months where the commission has credited this system.
>
> For anyone in corporate finance or M&A advisory, the takeaway is clear: the enforcement net is getting smarter, and the old assumption that small-ish trades fly under the radar is outdated. If you're in a compliance role, this is worth flagging to your team.

## Anti-patterns (do NOT do these)

- One-sentence headline summary ("SEC charges executive with insider trading.")
- Bullet-point list of facts with no narrative
- Copy-pasting the article abstract
- Generic filler ("This is an important development that could have far-reaching implications.")
- Skipping this step entirely and passing an empty `--summary ""`
