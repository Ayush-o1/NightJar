# Gateway overhead and cache latency

What NightJar adds to a request, and what a cache hit removes, measured against a **fake provider whose latency is fixed at 50 ms**. Reproduce with:

```bash
uv run python benchmarks/gateway_bench.py --json docs/benchmarks/gateway_bench_results.json
```

Raw output: [gateway_bench_results.json](gateway_bench_results.json).

## Conditions

| | |
|---|---|
| Machine | Apple M4, 10 cores, 16 GB, macOS (arm64); everything on one host over loopback |
| Python | 3.11.18; NightJar under uvicorn, **1 worker**, auth off, log level WARNING |
| Provider | `benchmarks/fake_upstream.py` (Python/uvicorn), fixed 50 ms delay, returns instantly after it |
| Redis | local `redis-server` 8.10.3, no persistence |
| Load | httpx, one Python process, closed loop (each worker sends its next request when the previous finishes), 400 requests per scenario after a 20-request warm-up |
| Requests | single short user message, `temperature: 0`, `model: gpt-4o` through LiteLLM's OpenAI path |
| Runs | **one run per scenario.** No repetitions, so no confidence intervals. Differences of a few milliseconds should not be over-read. |

## Results

Latency in ms at the 50th / 95th / 99th percentile; error rate was 0 in every scenario.

### Semantic tier off (exact cache only)

| Scenario | c=1 | c=16 |
|---|---|---|
| Direct to the fake provider (baseline) | 53.8 / 54.8 / 55.8 (18.6 rps) | 53.2 / 61.9 / 66.9 (292 rps) |
| NightJar, miss path (`X-NightJar-Cache: bypass`) | 68.3 / 70.5 / 78.8 (14.7 rps) | 86.4 / 105.9 / 164.3 (178 rps) |
| NightJar, exact-cache hit | **0.5 / 0.8 / 1.2** (1,678 rps) | 7.5 / 52.9 / 83.7 (1,000 rps) |

- **Gateway overhead on a miss at c=1 is about 14.5 ms** (68.3 vs 53.8 p50), roughly 27% on a 50 ms provider. On a real provider answering in hundreds of milliseconds to seconds it is a few percent; on a very fast model it is not negligible.
- **A hit at c=1 costs about 0.5 ms** end to end, instead of the 54 ms the provider call took. That is the exact-cache benefit, and it is also the *whole* point of caching: the hit rate on real traffic decides what that is worth, which this benchmark cannot say.
- During the hit scenarios the fake provider received **0** further requests, confirming hits never reach it.

### Semantic tier on (loads `all-MiniLM-L6-v2`)

| Scenario | c=1 | c=16 |
|---|---|---|
| Unique prompts: miss + embed + search + store | 80.2 / 84.3 / 85.8 (12.5 rps) | 122.6 / 162.3 / 193.3 (129 rps) |
| Paraphrase served from the semantic tier | **5.1 / 9.6 / 10.3** (173 rps) | not run |

A semantic hit costs about 5 ms (dominated by one embedding on CPU) versus 0.5 ms for an exact hit. Enabling the tier adds about 12 ms to a miss at c=1 (80.2 vs 68.3). Process memory (RSS, one sample each): about **280–320 MB** with the semantic tier off (mostly LiteLLM's imports; the embedding model is not loaded) and about **600 MB** with it on.

### What was found and changed because of these numbers

A miss with the semantic tier on originally embedded the prompt twice (once in `lookup`, once in `store`). The first full run measured the miss at 91.9 ms (c=1) and 197.9 ms (c=16); after reusing the lookup's vector in `store`, 80.2 and 122.6 ms. Single runs each, but both moved the way the profile predicted.

## Where the miss-path overhead comes from (partly)

Measured separately, same machine, same fake:

| Piece | p50 |
|---|---|
| Direct httpx call to the fake | 54.2 ms |
| LiteLLM `ProxyClient.forward` in-process to the fake | 60.8 ms (**+6.5 ms** for LiteLLM and its HTTP client) |
| Whole NightJar app in-process, provider stubbed to return instantly | **0.69 ms** (validation, routing, fingerprint, serialization, telemetry write) |
| Measured through real uvicorn | 68.3 ms (**+14.5 ms**) |

So NightJar's own code is about 0.7 ms, LiteLLM about 6.5 ms, and **roughly 7 ms of the 14.5 ms is not attributed**. Candidates are the extra HTTP hop and server parsing in uvicorn, thread hand-offs for telemetry, and log formatting. I did not profile the running server further, so none of those is established.

## What the numbers do not show

- **The c=64 rows are not interpretable and are omitted from the tables above** (they are in the JSON). "Direct to provider" at c=64 was *slower* than going through NightJar (143 vs 305 rps, p99 2.3 s), which cannot be true of a pure overhead measurement. The single-process fake provider and the single-process Python load generator saturate there; I did not separate them. The same applies to the hit path: its ~1,000–1,700 rps is a **lower bound** of what the gateway can do, limited by either the single-worker gateway or the single-process client. A proper throughput ceiling needs a multi-process or compiled load generator (for example `wrk` or `oha`), which I did not use.
- **No real provider.** No credentials were available, so real latency, streaming behaviour with real models, real token counts and real cost savings are unmeasured. "Cache saves money" is shown mechanically (0 provider calls on a hit), not as a dollar figure.
- **The hit rate on your traffic** decides everything above; the benchmark sends one hot prompt, the best case.
- **Single machine, loopback, 1 worker, one run.** Network latency, TLS and multiple workers change the numbers. Resource utilization was recorded only as process memory (RSS), not CPU.
- **Not measured:** sustained load over minutes, SQLite write contention at high request rates, memory growth over a long run, FAISS/numpy search time at the 50,000-entry cap inside the full gateway (the index alone was benchmarked in ADR-009).
