#!/usr/bin/env python
"""Measure the semantic cache's false-hit rate on labeled prompt pairs.

Each line of the pairs file is {"a": ..., "b": ..., "same": bool, "category": str}.
``same=true``: a cached answer to ``a`` is acceptable for ``b``. ``same=false``: serving it would be wrong.

For each threshold, reports with and without the lexical guards (app/guards.py):
  recall      share of same=true pairs that would be served from cache
  false hits  share of same=false pairs that would be served (the number that matters)

This is also the calibration tool for your own traffic: pass --pairs with pairs from your domain.
The default file is small and hand-written; read docs/benchmarks/CACHE_CORRECTNESS.md for its limits.

    uv run python benchmarks/cache_eval.py [--pairs benchmarks/cache_pairs.jsonl] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.guards import disagreement  # noqa: E402

THRESHOLDS = [0.70, 0.75, 0.80, 0.85, 0.90, 0.92, 0.95, 0.97]
DEFAULT_THRESHOLD = 0.92


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(pairs: list[dict], sims: list[float]) -> dict:
    guard = [disagreement(p["a"], p["b"]) for p in pairs]
    same = [p["same"] for p in pairs]
    n_same, n_diff = sum(same), len(same) - sum(same)
    rows = []
    for t in THRESHOLDS:
        row = {"threshold": t}
        for name, use_guard in (("raw", False), ("guarded", True)):
            served = [s >= t and not (use_guard and g) for s, g in zip(sims, guard, strict=True)]
            row[name] = {
                "recall": sum(sv and sm for sv, sm in zip(served, same, strict=True)) / n_same,
                "false_hit_rate": sum(sv and not sm for sv, sm in zip(served, same, strict=True)) / n_diff,
                "false_hits": sum(sv and not sm for sv, sm in zip(served, same, strict=True)),
            }
        rows.append(row)

    by_cat: dict[str, dict] = defaultdict(lambda: {"n": 0, "raw": 0, "guarded": 0})
    for p, s, g in zip(pairs, sims, guard, strict=True):
        if p["same"]:
            continue
        c = by_cat[p["category"]]
        c["n"] += 1
        c["raw"] += s >= DEFAULT_THRESHOLD
        c["guarded"] += s >= DEFAULT_THRESHOLD and not g
    same_sims = [s for s, m in zip(sims, same, strict=True) if m]
    diff_sims = [s for s, m in zip(sims, same, strict=True) if not m]
    return {
        "n_same": n_same, "n_different": n_diff, "thresholds": rows, "by_category_at_default": dict(by_cat),
        "similarity": {
            "same_min": min(same_sims), "same_median": float(np.median(same_sims)),
            "diff_max": max(diff_sims), "diff_median": float(np.median(diff_sims)),
        },
    }


def to_markdown(res: dict, model: str) -> str:
    out = [
        f"Embedding model: `{model}`. {res['n_same']} should-match pairs, "
        f"{res['n_different']} should-not-match pairs.\n",
        "| threshold | recall (raw) | false hits (raw) | recall (guarded) | false hits (guarded) |",
        "|---|---|---|---|---|",
    ]
    for r in res["thresholds"]:
        raw, g = r["raw"], r["guarded"]
        out.append(f"| {r['threshold']:.2f} | {raw['recall']:.0%} | {raw['false_hits']} / {res['n_different']} "
                   f"({raw['false_hit_rate']:.0%}) | {g['recall']:.0%} | {g['false_hits']} / {res['n_different']} "
                   f"({g['false_hit_rate']:.0%}) |")
    out += [
        f"\nFalse hits by category at threshold {DEFAULT_THRESHOLD}:\n",
        "| category | pairs | served raw | served guarded |",
        "|---|---|---|---|",
    ]
    for cat, c in res["by_category_at_default"].items():
        out.append(f"| {cat} | {c['n']} | {c['raw']} | {c['guarded']} |")
    s = res["similarity"]
    out.append(f"\nSimilarity, should-match pairs: min {s['same_min']:.3f}, median {s['same_median']:.3f}. "
               f"Should-not-match pairs: median {s['diff_median']:.3f}, **max {s['diff_max']:.3f}**.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", type=Path, default=Path(__file__).parent / "cache_pairs.jsonl")
    ap.add_argument("--json", type=Path, help="also write results as JSON")
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    args = ap.parse_args()

    from app.embedder import Embedder

    emb = Embedder(args.model)
    pairs = load(args.pairs)
    va = emb.embed_batch([p["a"] for p in pairs])
    vb = emb.embed_batch([p["b"] for p in pairs])
    sims = [float(x) for x in np.sum(va * vb, axis=1)]
    res = evaluate(pairs, sims)
    res["model"], res["pairs_file"] = args.model, str(args.pairs)
    print(to_markdown(res, args.model))
    if args.json:
        args.json.write_text(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
