# Ranking Criteria Reference

How `rank_and_summarize.py` scores and ranks items in the knowledge base.

## Scoring Formula

```
score = (0.4 × recency) + (0.4 × relevance) + (0.2 × authority)
```

Each component produces a value between 0.0 and 1.0.

## Components

### Recency (40%)

Measures how recently the item was saved. Uses exponential decay:

```
recency = exp(-0.02 × hours_since_saved)
```

| Age | Score |
|-----|-------|
| Just saved | 1.00 |
| 6 hours | 0.89 |
| 12 hours | 0.79 |
| 24 hours | 0.62 |
| 48 hours | 0.38 |
| 72 hours | 0.24 |
| 1 week | 0.04 |

Half-life is approximately 35 hours.

### Relevance (40%)

Measures topic overlap between the item and user's configured interests. Uses Jaccard similarity:

```
relevance = |item_terms ∩ config_terms| / |item_terms ∪ config_terms|
```

Where:
- `item_terms` = words from the item's topics + title
- `config_terms` = words from user's configured topics + sub-fields

### Authority (20%)

Binary score based on whether the source is a known authoritative publication:

- **1.0** — Source domain is in the authoritative list (Reuters, Bloomberg, SEC.gov, Nature, etc.)
- **0.5** — Source domain is not recognized (not penalized heavily, just lower confidence)

### Authoritative Sources

The following domains receive a 1.0 authority score:

- **News:** reuters.com, bloomberg.com, ft.com, wsj.com, nytimes.com, apnews.com, bbc.com, cnn.com, theguardian.com
- **Legal:** sec.gov, law360.com, scotusblog.com
- **Government:** federalreserve.gov, fda.gov, nih.gov, politico.com, thehill.com
- **Science:** nature.com, science.org, nejm.org, lancet.com
- **Technology:** techcrunch.com, wired.com, arstechnica.com
- **Healthcare:** statnews.com

## Example Scoring

An SEC enforcement article from Reuters, saved 6 hours ago, matching 3 of the user's 5 configured topics:

```
recency   = exp(-0.02 × 6)  = 0.89
relevance = jaccard(...)     = 0.60  (approximate, depends on term overlap)
authority = 1.0              (reuters.com is authoritative)

score = (0.4 × 0.89) + (0.4 × 0.60) + (0.2 × 1.0)
      = 0.356 + 0.240 + 0.200
      = 0.796 → displayed as "Relevance: 80%"
```

## Customization

The default weights (0.4 / 0.4 / 0.2) are hardcoded in `rank_and_summarize.py`. To adjust priorities:

- Increase recency weight to surface newer content first
- Increase relevance weight to prioritize topic-matched content
- Increase authority weight to favor established sources

The authoritative domains list can be extended in `rank_and_summarize.py` by adding to the `AUTHORITATIVE_DOMAINS` set.
