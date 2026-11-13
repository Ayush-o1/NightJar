# Architecture

How NightJar works internally. For endpoint schemas see [API.md](API.md); for why things were built this way see [DECISIONS.md](DECISIONS.md).

---

## Overview

NightJar is an OpenAI-compatible proxy that sits between an app and upstream LLM providers. Apps point their `base_url` at it and change nothing else. Every upstream call goes out through LiteLLM, which handles per-provider auth, retries, and failover.

Three things happen before a request leaves the building:

1. **Model routing** — cheap prompts go to a cheap model, expensive ones to a strong model
2. **Context compression** — long conversations get their older turns summarized
3. **Semantic caching** — near-duplicate prompts are answered from cache instead of the provider

---

## Request pipeline

```
POST /v1/chat/completions
        │
        ▼
  Validate body ─────────────── Pydantic (app/models.py)
        │
        ▼
  Route ────────────────────── token count + keywords → simple | complex
        │                       (app/router.py, config/routing_rules.yaml)
        │                       stream=true short-circuits here → LiteLLM
        ▼
  Compress ────────────────── only if tokens > threshold AND turns > min
        │                       (app/compressor.py; skip via X-NightJar-No-Compress)
        ▼
  Cache lookup ───────────── embed → FAISS search → Redis fetch
        │                     (app/cache.py, threshold auto-tuned by app/adaptive.py)
        │
   HIT ─┴─ MISS
    │      │
    │      ▼
    │   LiteLLM Router ────── provider call, retries + failover (app/proxy.py)
    │      │
    │      ▼
    │   Cache store ───────── embed → FAISS add + Redis set (TTL)
    │      │
    └──┬───┘
       ▼
  Telemetry write ────────── one SQLite row per request (app/middleware.py)
       │
       ▼
  Response + X-Cache, X-Model-Tier, X-Model-Selected,
             X-Compressed, X-Compression-Ratio, X-Similarity
```

**Streaming** (`"stream": true`) skips compression and caching entirely — a token stream can't be matched against a cache before it exists, and buffering it to compress would defeat the point of streaming. It still gets routed and still writes telemetry.

---

## Components

| Layer | Responsibility | Files |
|-------|---------------|-------|
| API gateway | Validates requests, orchestrates the pipeline | `app/main.py`, `app/models.py` |
| Model router | Classifies prompt complexity, picks the model tier | `app/router.py`, `config/routing_rules.yaml` |
| Context compressor | Summarizes older turns to cut token count | `app/compressor.py` |
| Semantic cache | Embeds prompts, searches FAISS, reads/writes Redis | `app/cache.py`, `app/embedder.py`, `app/vector_store.py` |
| Upstream client | Forwards to providers with retries + failover | `app/proxy.py` |
| Telemetry | Per-request SQLite row; aggregation queries | `app/telemetry.py`, `app/costs.py`, `app/middleware.py` |
| Adaptive threshold | Tunes the cache similarity threshold from hit rates | `app/adaptive.py` |
| Auth | Opt-in bearer-token check | `app/auth.py` |
| Dashboard | Reads the telemetry API, renders it | `docs/dashboard/` |
| Benchmarks | Offline routing-accuracy / cache / latency measurement | `benchmarks/` |

**Concurrency note:** `VectorStore` guards all FAISS writes with a `threading.Lock` — FAISS index objects aren't thread-safe, and Uvicorn serves requests concurrently. SQLite runs in WAL mode so telemetry writes don't block reads.

---

## Data model

Two SQLite tables in `./data/telemetry.db`, created at startup.

**`telemetry`** — one row per request, written by `TelemetryMiddleware`. Includes cache hits, which never reach a provider.

```sql
CREATE TABLE telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT UNIQUE,
    timestamp           DATETIME,
    model_requested     TEXT,
    model_used          TEXT,
    tier                TEXT,    -- 'simple' | 'complex'
    routing_reason      TEXT,    -- 'token_count:150<=200', 'complex_keyword:analyze', ...
    cache_hit           BOOLEAN,
    similarity_score    REAL,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    estimated_cost_usd  REAL,
    latency_ms          REAL,
    compressed          BOOLEAN,
    compression_ratio   REAL
);
CREATE INDEX idx_telemetry_timestamp ON telemetry(timestamp);
```

The index exists because every read path (`GET /v1/telemetry`, the dashboard's time-range filter) sorts by `timestamp DESC`; without it those are full table scans.

`tier` and `routing_reason` are stored, not just returned in response headers, so routing behaviour can be analyzed after the fact — that's what lets the dashboard show a real tier breakdown instead of a made-up one.

**`request_log`** — written by the LiteLLM success callback in `proxy.py`, only for calls that actually hit a provider. Carries real cost from `litellm.completion_cost()`, which is why `/admin/usage` and `/admin/savings` read from here rather than from the estimated costs in `telemetry`.

Cache hits appear in `telemetry` but never in `request_log`. That split is deliberate: `telemetry` answers "what did the gateway do?", `request_log` answers "what did we actually pay for?".

**`threshold_history`** — one row per adaptive-threshold evaluation (`threshold`, `cache_hit_rate`, `evaluated_at`).

---

## Semantic cache

Prompts are embedded locally with `all-MiniLM-L6-v2` (384-dim, CPU, no API call) and searched against a FAISS `IndexFlatIP` index. Vectors are L2-normalized, so inner product equals cosine similarity. A hit above the active threshold fetches the stored response from Redis by content hash.

**Redis is an optimization, not a dependency.** `lookup()` and `store()` catch Redis errors and degrade to a cache miss — a Redis outage makes the gateway slower and more expensive, not broken. `/health` reports Redis reachability separately from process liveness for this reason.

**Adaptive threshold** — `ThresholdManager` reads recent hit rates from `telemetry` and nudges the similarity threshold: above 60% hit rate it tightens by 0.01 (the cache is probably serving loose matches), below 20% it loosens, clamped to `[min, max]`.

---

## Dashboard

Static HTML/CSS/JS in `docs/dashboard/` — no build step, no framework. Served at `/dashboard/` by the app, or openable as a file.

```
docs/dashboard/
├── index.html
├── css/style.css
└── js/
    ├── data.js      # demo dataset — used only when the backend is unreachable
    ├── ui.js        # toast, modal, sidebar, connection check, formatters
    ├── charts.js    # Chart.js setup
    ├── tables.js    # HTML escaping, table rendering, pagination, filters
    └── app.js       # navigation, data loading + aggregation, button handlers
```

Pages: **Overview** (summary cards, trends, recent requests), **Requests** (filterable log + CSV export), **Cache** (FAISS/Redis stats, similarity distribution, recent hits), **Router** (tier split, routing reasons), **Telemetry** (cost/latency/hit-rate trends), **Threshold** (current vs. baseline, evaluation history, manual evaluate).

On narrow screens the tables re-render as stacked records rather than scrolling sideways — the cache result and latency of a request are the point of the row, and they shouldn't be the columns you have to drag to reach.

Uses `GET /health`, `GET /v1/telemetry`, `GET /v1/telemetry/summary`, `GET /v1/cache/stats`, `GET /v1/threshold`, `GET /v1/threshold/history`, `POST /v1/threshold/evaluate`, `DELETE /v1/cache`. If `NIGHTJAR_API_KEYS` is set, all of them except `/health` need a bearer token — set one with `localStorage.setItem('nightjar_api_key', 'your-token')`.

Decisions worth knowing about:

**Colour carries meaning, or it isn't used.** An earlier version painted every latency green and every cache `MISS` red, which made a miss — the normal path — look like an error and left colour carrying no signal at all. Now the default state is neutral: cache *hits* are green because they saved a call, latency only colours once it's slow enough to care about, and red is reserved for destructive actions. Tiers use a neutral/violet pair rather than green/red, since "complex" is a routing outcome, not a failure.

**One aggregation path for real and demo data.** `aggregateByDay()`, `aggregateByReason()`, `tierCounts()`, and `similarityScoresFromHits()` in `app.js` run over a plain list of requests, whichever source it came from. The demo dataset can't drift from what the real dashboard shows, because there's no second code path for it to drift in.

**No live "routing accuracy".** Accuracy needs a ground-truth label — "was `complex` the right call for this prompt?" — and production traffic doesn't come with one. The only real accuracy number comes from the offline benchmark against the labeled 1,000-prompt set (`benchmarks/`). The Router page shows the live tier split and routing reasons instead of inventing a number the system has no way to know.

**No cache-entry browser.** The backend stores response hashes and FAISS vectors, never prompt text, so there is genuinely no per-entry list to render. The Cache page shows aggregate stats plus which recent requests were served from cache.

**Escaping is load-bearing.** `model_used` is attacker-controllable — a client can set it through the `X-NightJar-Model-Override` header, which the router passes through unsanitized. Every dynamic value goes through `escapeHtml()` in `tables.js` before reaching `innerHTML`. Don't reintroduce raw interpolation for these fields.

---

## Security

| Concern | Approach |
|---------|----------|
| Gateway auth | Opt-in bearer token (`app/auth.py`) on everything except `/health`, enabled by setting `NIGHTJAR_API_KEYS`; constant-time comparison. Off by default for local dev — see ADR-005. |
| CORS | Origins configurable via `CORS_ALLOW_ORIGINS`; credentialed (cookie) CORS is never enabled, so a wildcard origin carries no CSRF risk. |
| XSS | All dynamic values escaped before HTML insertion in the dashboard (see above). |
| Secrets | Provider keys live in `.env` (gitignored), mapped into `os.environ` for LiteLLM at startup; never logged or echoed in responses. |
| Data locality | Prompts, responses, and telemetry stay on the machine — SQLite file, local Redis, local embedding model. Nothing is sent anywhere except the LLM provider the caller chose. |
| Redis outage | Degrades to a cache miss instead of failing the request. |

---

## Stack

FastAPI + Uvicorn · LiteLLM · sentence-transformers (`all-MiniLM-L6-v2`) · FAISS (CPU) · Redis 7 · SQLite (WAL) · tiktoken · Pydantic Settings · structlog · pytest + ruff · Chart.js (dashboard) · Docker Compose (local dev).

Pinned versions live in `requirements.txt`.

---

## Decisions

| ADR | Decision |
|-----|----------|
| ADR-001 | FAISS over Qdrant for the vector index |
| ADR-002 | Rule-based classifier for routing |
| ADR-003 | SQLite for telemetry |
| ADR-004 | all-MiniLM-L6-v2 as the embedding model |
| ADR-005 | Opt-in bearer-token auth over a mandatory auth layer |
| ADR-006 | E2E tests isolated by pytest marker, not skip-on-missing-key |

Full context and upgrade paths in [DECISIONS.md](DECISIONS.md).
