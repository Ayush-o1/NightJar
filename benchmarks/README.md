# Benchmarks

| Script | Needs | Measures |
|---|---|---|
| `gateway_bench.py` | Redis binary on PATH | Gateway overhead, exact-hit and semantic-hit latency, throughput and errors against a fake provider. `--smoke` is a 10-second run used in CI (fails on errors, never on speed). Results and limits: [docs/benchmarks/GATEWAY_OVERHEAD.md](../docs/benchmarks/GATEWAY_OVERHEAD.md) |
| `cache_eval.py` | The embedding model | False-hit rate and recall of the semantic cache on labeled prompt pairs; also the tool for calibrating `SIMILARITY_THRESHOLD` on your own data. Results: [docs/benchmarks/CACHE_CORRECTNESS.md](../docs/benchmarks/CACHE_CORRECTNESS.md) |
| `fake_upstream.py` | nothing | A fake OpenAI-compatible provider (latency, failure injection, call counters). Also used by the test suite. |
| `run_benchmark.py` | A running NightJar and a **real** provider key | Routing agreement with `prompts_labeled.json`, cache hit rate on paraphrases, latency percentiles. Legacy; costs real money. |

`run_benchmark.py` used to have a `--dry-run` mode that generated its results from a seeded random number generator and asserted on them (it is where the old README's "~88% routing accuracy" came from). It was removed. Nothing here prints a number that was not measured.

Data: `prompts_labeled.json` (1,000 short prompts labeled simple/complex; see the caveats in [docs/BASELINE.md](../docs/BASELINE.md)), `cache_pairs.jsonl` (71 labeled prompt pairs).
