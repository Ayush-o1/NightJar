# Scalability

What limits NightJar as load or data grows, what has been measured, and what would change. Numbers come from [../benchmarks/GATEWAY_OVERHEAD.md](../benchmarks/GATEWAY_OVERHEAD.md) (one machine, one run, fake provider) and [ADR-009](../DECISIONS.md); anything not measured is labelled.

## Single process

| Resource | Behaviour | Limit / evidence |
|---|---|---|
| Event loop | Handlers are async; CPU-bound work is bounded or moved to threads | The gateway's own per-request code costs about 0.7 ms (measured). LiteLLM adds about 6.5 ms. |
| Cache hits | Redis `GET` + JSON decode | At least ~1,000 rps through one uvicorn worker, one client process (a lower bound; see the benchmark's limits) |
| Provider calls | Bounded by `MAX_CONCURRENT_UPSTREAM` (503 beyond it) | Default 128 |
| Semantic search | Brute-force cosine inside one partition | 0.1–3 ms at 10k–100k vectors (ADR-009); embedding (~5 ms on CPU) dominates |
| Memory | Vector matrix `entries × 384 × 4 B` (150 MB at 100k) plus the embedding model when the semantic tier is on | Entries capped by `CACHE_MAX_ENTRIES` (50,000 → about 77 MB); measured RSS: about 280–320 MB (semantic off), about 600 MB (semantic on) |
| SQLite | One short transaction per request, in a thread | Not benchmarked at high write rates |

## What stops a second replica from just working

| State | Shared? | Effect with 2 replicas |
|---|---|---|
| Cached responses | Yes (Redis) | Both serve each other's exact hits |
| Vector index | No (local file) | Each replica's semantic tier only sees what it stored itself |
| Single-flight table | No | Identical concurrent requests on *different* replicas both call the provider |
| Concurrency limit, rate limit | No | Effective limits are N times the configured ones |
| Telemetry, key store | No (SQLite file per replica) | Split data; a key created on one replica does not exist on the other |
| Provider cooldown state | No (in memory) | Each replica discovers a failing provider separately |

So **horizontal scaling is not supported today.** `uvicorn --workers N` on one host has the same problem (separate processes), so the supported shape is one process.

## Upgrade paths, in the order I would take them

1. **Throughput on one box:** measure first with a real load generator, then profile the running server (the ~7 ms unattributed overhead is the obvious place to look). Do not add workers before deciding what to do about the per-process state above.
2. **Shared state without a rewrite:** move the key store and telemetry to Postgres (the access code is isolated in `app/keys.py` and `app/telemetry.py`), keep exact-cache payloads in Redis, and either route by tenant to a fixed replica or accept a per-replica semantic tier.
3. **Shared semantic index:** a vector store that supports filtered search (Qdrant, pgvector). Required past roughly a million vectors per partition anyway (ADR-009).
4. **Shared rate limits and single-flight:** Redis (token bucket scripts; `SET NX` locks). A cross-replica single-flight needs care with leader failure and TTLs.
5. **Embedding off the box:** only if profiling shows embedding saturating a core: a small worker pool, then a separate service.

None of these is built, and none should be built before there is a multi-replica requirement to test it against.
