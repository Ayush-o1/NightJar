# Cache correctness

How often does the semantic cache serve an answer that is wrong for the question asked? Measured with the shipped embedding model on a labeled pair set. Reproduce:

```bash
uv run python benchmarks/cache_eval.py --json docs/benchmarks/cache_eval_results.json
```

Raw output is in [cache_eval_results.json](cache_eval_results.json). The pairs are in [benchmarks/cache_pairs.jsonl](../../benchmarks/cache_pairs.jsonl).

## Method

71 prompt pairs, each labeled by me:

- **30 should-match** (paraphrases: a cached answer to `a` is fine for `b`).
- **41 should-not-match** (the answers differ): number changes (8), proper-noun swaps (8), negations (5), lowercase word swaps such as ascending/descending or ibuprofen/acetaminophen (12), related-but-different intents such as "reset my password" / "reset my username" (8).

For each pair: cosine similarity from `all-MiniLM-L6-v2`; a pair is "served" if similarity is at or above the threshold. "Guarded" additionally requires that `app/guards.py` finds no disagreement on numbers, negation, quoted text or proper nouns. **False hit** = a should-not-match pair that is served. **Recall** = share of should-match pairs served.

## Results

| threshold | recall (raw) | false hits (raw) | recall (guarded) | false hits (guarded) |
|---|---|---|---|---|
| 0.70 | 100% | 32 / 41 (78%) | 93% | 14 / 41 (34%) |
| 0.75 | 100% | 28 / 41 (68%) | 93% | 10 / 41 (24%) |
| 0.80 | 100% | 22 / 41 (54%) | 93% | 6 / 41 (15%) |
| 0.85 | 87% | 15 / 41 (37%) | 80% | 5 / 41 (12%) |
| 0.90 | 80% | 10 / 41 (24%) | 73% | 3 / 41 (7%) |
| **0.92 (default)** | 70% | **9 / 41 (22%)** | 63% | **3 / 41 (7%)** |
| 0.95 | 50% | 6 / 41 (15%) | 47% | 2 / 41 (5%) |
| 0.97 | 20% | 3 / 41 (7%) | 20% | 1 / 41 (2%) |

At the default threshold, by category:

| category | pairs | served raw | served guarded |
|---|---|---|---|
| number_change | 8 | 2 | 0 |
| proper_noun_swap | 8 | 0 | 0 |
| negation | 5 | 4 | 0 |
| lowercase_swap | 12 | 3 | 3 |
| related_intent | 8 | 0 | 0 |

Similarity of should-match pairs: min 0.821, median 0.952. Of should-not-match pairs: median 0.813, **max 0.992**.

## What this says

1. **A threshold alone cannot make the semantic cache safe.** The most dangerous wrong pair ("10 miles to kilometers" / "10 kilometers to miles") scores 0.992, higher than the median true paraphrase. Every threshold that keeps a useful share of paraphrases still serves wrong answers; the one that serves none (0.97) also loses 80% of the paraphrases.
2. **The guards help a lot, and only for what they can see.** At 0.92 they take false hits from 9 to 3 and remove every number, negation and proper-noun error. The three that remain are all lowercase word swaps (miles/kilometers, ascending/descending, ibuprofen/acetaminophen). Those need meaning, not pattern matching, and the guards do not have it.
3. **Hence the design:** the exact tier is always on; the semantic tier is **opt-in** (`SEMANTIC_CACHE=true`), restricted to identical model, parameters and conversation context, with these guards. If a wrong answer is expensive in your domain (medical, legal, financial, code that gets executed), leave it off.
4. **The old adaptive threshold would have made this worse.** It lowered the threshold toward 0.70 whenever the hit rate was under 20%. The first row shows what that admits: 78% of should-not-match pairs.

## Limits of this measurement

- **71 pairs written by one person.** The 41 negatives give wide uncertainty: 3 / 41 is a 7% point estimate with a plausible range of roughly 2–20%. Do not quote it as "7%" without that.
- **The categories and the guards were designed together.** I knew what the guards check when I chose the negative categories, so the guarded numbers are optimistic. A different author would find failure types I didn't think of.
- **Short, English, general-knowledge prompts.** Long prompts, code, other languages and domain jargon behave differently, and may be better or worse.
- **One embedding model.** Different models separate these cases differently (the research cited in [SOURCE_REFERENCES.md](../research/SOURCE_REFERENCES.md) reports the best threshold varies by model).
- **It measures answer equivalence by label, not by comparing model outputs.** Two questions with different wording but the same correct answer count as should-match only if I labeled them so.

## Use it on your own traffic

The file format is one JSON object per line: `{"a": "...", "b": "...", "same": true, "category": "..."}`. Collect pairs from your own logs (near-duplicates from real traffic, plus the near-misses that worry you), run the script with `--pairs`, and choose a threshold from *your* table. That replaces the adaptive controller, which adjusted the threshold from hit rate and so could not see correctness.
