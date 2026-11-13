# NightJar

**An OpenAI-compatible LLM proxy that cuts cost and latency without touching your app code.**

Point your client at `localhost:8000` instead of `api.openai.com`. NightJar sits in between and applies semantic caching, rule-based model routing, and context compression before forwarding the request through LiteLLM to whichever provider you configured.

[![CI](https://github.com/Ayush-o1/NightJar/actions/workflows/ci.yml/badge.svg)](https://github.com/Ayush-o1/NightJar/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Why

An app that calls an LLM API directly pays full price for every request. It pays again for the same question asked slightly differently, and it pays GPT-4o rates to answer "hi". Fixing that in application code means touching every call site and re-doing it in every service.

A proxy is a better place for it: the app changes one URL, and caching, routing, and compression apply to everything that goes through. That also makes it the natural place to collect per-request telemetry, which is otherwise scattered across whatever each provider's dashboard happens to show.

---

## What it does

1. **Model routing** — classifies each prompt as `simple` or `complex` from token count and keyword rules, then picks the matching model tier (e.g. `gpt-3.5-turbo` vs `gpt-4o`). Rules live in `config/routing_rules.yaml`. Measured at ~88% accuracy against a labeled 1,000-prompt set.

2. **Semantic caching** — prompts are embedded locally with `all-MiniLM-L6-v2` and matched against a FAISS index. Above the similarity threshold (default 0.92), the cached response comes back from Redis with no upstream call. The threshold auto-tunes itself from observed hit rates.

3. **Context compression** — once a conversation passes a token threshold and a minimum turn count, older turns get summarized so the upstream request stays small.

4. **Multi-provider forwarding** — cache misses go out through LiteLLM Router, which handles provider auth, retries, and failover across OpenAI / Anthropic / Gemini / Groq / Mistral / Ollama and others. Switching provider is a model-string change.

5. **Telemetry + dashboard** — every request is logged to SQLite (model, tier, routing reason, latency, cost, cache hit, compression ratio) and exposed through an admin API and a static dashboard.

The embedding model and the classifier both run locally on CPU — no third-party service sees your prompts except the LLM provider you explicitly route to.

---

## Architecture

```
POST /v1/chat/completions
     │
     ▼
  Route ──── token count + keywords → simple | complex
     │
     ▼
  Compress ──── summarize old turns if the conversation is long
     │
     ▼
  Cache lookup ──── embed → FAISS → Redis
     │
 HIT ┴ MISS ──→ LiteLLM Router ──→ Provider API
     │              │
     │          Cache store
     └──────┬───────┘
            ▼
     Telemetry write (SQLite) → response + diagnostic headers
```

Streaming requests skip caching and compression and are forwarded straight through.

Full detail — components, data model, concurrency, dashboard design, threat model — in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Design rationale in [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Stack

| Component | Choice |
|-----------|--------|
| Web framework | FastAPI (Python 3.11) + Uvicorn |
| Provider gateway | LiteLLM Router |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` (384-dim, CPU) |
| Vector search | FAISS `IndexFlatIP` |
| Cache store | Redis 7 |
| Telemetry DB | SQLite (WAL mode, raw `sqlite3`) |
| Config | Pydantic Settings + `.env` |
| Logging | structlog |
| Tests / lint | pytest, ruff |
| Dashboard | Vanilla HTML/CSS/JS + Chart.js |

---

## Setup

**Prerequisites:** Python 3.11+, Redis, and at least one LLM provider API key.

### Docker (simplest — brings up Redis too)

```bash
git clone https://github.com/Ayush-o1/NightJar.git
cd NightJar

cp .env.example .env      # add at least one provider key

docker compose up --build -d
curl http://localhost:8000/health
```

### Local

```bash
uv sync --frozen          # exact versions from uv.lock; see docs/operations/DEPENDENCIES.md

cp .env.example .env      # add at least one provider key
docker run -d -p 6379:6379 redis:7-alpine

uv run uvicorn app.main:app --port 8000 --reload
```

First start downloads the embedding model (~80 MB) and caches it.

### Usage

Change the `base_url`, nothing else:

```python
import openai

client = openai.OpenAI(base_url="http://localhost:8000/v1", api_key="your-key")

response = client.chat.completions.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "What is the capital of France?"}],
)
```

Responses carry diagnostic headers: `X-Cache` (HIT/MISS), `X-Similarity`, `X-Model-Tier`, `X-Model-Selected`, `X-Compressed`, `X-Compression-Ratio`.

Two request headers are recognized: `X-NightJar-Model-Override` (force a model, bypass routing) and `X-NightJar-No-Compress: true`.

---

## Configuration

Every variable is documented inline in [`.env.example`](.env.example). The ones you're most likely to touch:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` etc. | `""` | Provider keys — set at least one (`ANTHROPIC_`, `GEMINI_`, `GROQ_`, `MISTRAL_`, `COHERE_`, `XAI_`) |
| `SIMPLE_MODEL` | `gpt-3.5-turbo` | Model for the simple tier |
| `COMPLEX_MODEL` | `gpt-4o` | Model for the complex tier |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `SIMILARITY_THRESHOLD` | `0.92` | Cosine similarity required for a cache hit |
| `CACHE_TTL_SECONDS` | `86400` | Cached response lifetime |
| `COMPRESS_THRESHOLD` | `2000` | Token count that triggers compression |
| `COMPRESS_MIN_TURNS` | `6` | Minimum turns before compression applies |
| `ADAPTIVE_THRESHOLD_ENABLED` | `true` | Auto-tune the similarity threshold |
| `NIGHTJAR_API_KEYS` | `""` | Comma-separated bearer tokens. Empty = auth off (local-dev default) |
| `CORS_ALLOW_ORIGINS` | `*` | Allowed origins; credentialed CORS is never enabled |
| `TEST_MODE` | `false` | Force the cheapest model for every request |

---

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions |
| `GET` | `/health` | Liveness + Redis reachability |
| `GET` | `/v1/telemetry` | Paginated request records |
| `GET` | `/v1/telemetry/summary` | Aggregated stats |
| `GET` | `/v1/threshold` | Current adaptive threshold |
| `GET` | `/v1/threshold/history` | Past threshold evaluations |
| `POST` | `/v1/threshold/evaluate` | Trigger a threshold re-evaluation |
| `GET` | `/v1/cache/stats` | FAISS vector count, Redis keys, active threshold |
| `DELETE` | `/v1/cache` | Flush the cache |
| `DELETE` | `/v1/cache/{key}` | Invalidate one entry |
| `GET` | `/admin/usage` | Spend/token summary (filterable) |
| `GET` | `/admin/logs` | Raw request log |
| `GET` | `/admin/savings` | Estimated savings from cache + routing |

Schemas and examples: [docs/API.md](docs/API.md).

**Auth is off by default.** Set `NIGHTJAR_API_KEYS` and every endpoint except `/health` requires `Authorization: Bearer <token>`. Do that before exposing this beyond localhost — it's a proxy holding your provider keys.

---

## Dashboard

Open `http://localhost:8000/dashboard/` while the backend is running.

Shows the request log, cache hit rate, model and tier distribution, latency and cost trends, routing-reason breakdown, and the adaptive threshold — all computed from real `/v1/*` responses. If it can't reach a backend it falls back to a demo dataset and says so in the header badge.

It's plain HTML, CSS and JavaScript with Chart.js — no build step, no framework. Tables collapse into stacked records on narrow screens, colour is only used where it means something (a cache hit saved a call; a slow request needs a look), and every screen has a real empty state instead of a blank panel.

---

## Testing

```bash
uv run ruff check app/ tests/ benchmarks/
PYTHONPATH=. uv run pytest tests/     # mocked — no network, no Redis needed
```

This is exactly what CI runs. The suite covers the router classifier (including accuracy against the labeled dataset), cache hit/miss and Redis-outage degradation, compression triggers and fallback, telemetry read/write, adaptive threshold bounds, auth on/off, failover, tool-call handling, and the proxy's error propagation.

**Live E2E tests** (`tests/test_e2e.py`) are marked `e2e` and excluded by default — they hit a real provider and cost real money:

```bash
RUN_E2E_TESTS=1 OPENAI_API_KEY=sk-real-key PYTHONPATH=. pytest tests/test_e2e.py -m e2e
```

**Benchmarks** measure routing accuracy, cache hit rate, and latency percentiles against the 1,000-prompt labeled set. See [benchmarks/README.md](benchmarks/README.md).

```bash
python benchmarks/run_benchmark.py --dry-run   # no server required
```

---

## Implementation notes

**Two telemetry tables, on purpose.** `telemetry` gets a row for every request including cache hits, with estimated costs. `request_log` is written by LiteLLM's success callback only when a provider was actually called, and carries real cost from `litellm.completion_cost()`. `telemetry` answers "what did the gateway do", `request_log` answers "what did we pay for". `/admin/usage` and `/admin/savings` read the latter.

**Redis is optional at runtime.** Cache reads and writes catch connection errors and degrade to a miss, so a Redis outage makes the gateway slower, not broken. `/health` reports Redis separately from process liveness.

**FAISS writes are locked.** FAISS index objects aren't thread-safe and Uvicorn handles requests concurrently, so `VectorStore` guards mutations with a `threading.Lock`. The index persists to `./data/faiss.index` on shutdown alongside an id-map file; the two are always written and deleted together to avoid an orphaned index.

**Streaming skips the cache.** You can't match a token stream against a cache before it exists, and buffering it to compress would defeat streaming. Streamed requests are still routed and still logged.

**The dashboard escapes everything.** `model_used` is attacker-controllable through the `X-NightJar-Model-Override` header, so every dynamic value is escaped before it reaches `innerHTML`.

---

## Troubleshooting

**`Fatal Python error: Aborted` on macOS (Apple Silicon)** — `faiss-cpu` and `torch` each bundle their own OpenMP runtime and abort when both load. Local-dev only; Linux/Docker are unaffected:

```bash
export KMP_DUPLICATE_LIB_OK=TRUE
```

**Startup hangs ~15s with no internet** — sentence-transformers pings Hugging Face even when the model is cached. Skip it with `HF_HUB_OFFLINE=1`.

**`no such table` or missing telemetry columns** — the schema is created at startup, so an old `./data/telemetry.db` from before a schema change won't have new columns. Delete it and restart; it's local runtime data, not source.

**`ModuleNotFoundError: No module named 'app'`** — run pytest with `PYTHONPATH=.`.

**Cache lookups behaving oddly** — delete `data/faiss.index` and `data/faiss.index.idmap` together and restart for a clean index.

---

## Known limitations

- **Single instance.** The FAISS index and SQLite database are local files, so the cache and telemetry don't span replicas. Scaling out means a shared vector store and Postgres — upgrade paths are written up in [docs/DECISIONS.md](docs/DECISIONS.md) (ADR-001, ADR-003).
- **Routing accuracy is measured offline only.** Live traffic has no ground-truth labels, so the ~88% figure comes from the labeled benchmark set, not production.
- **Compression costs a call.** Summarizing older turns is itself an LLM request, so it only pays off on genuinely long conversations — hence the token *and* turn-count gates.
- **Costs in `telemetry` are estimates** from a static per-model table that goes stale as providers change pricing. `request_log` has the real numbers.
- **One global similarity threshold**, not per-model or per-tenant.
- **No rate limiting or per-key quotas.** The auth layer is a flat token list, not an identity system.

---

## Project layout

```
app/            FastAPI service — routing, cache, compression, proxy, telemetry, auth
config/         routing_rules.yaml (thresholds, keywords, model tiers)
docs/           ARCHITECTURE.md, API.md, DECISIONS.md, dashboard/
tests/          pytest suite (mocked; live E2E opt-in)
benchmarks/     Routing/cache/latency benchmark runner + 1,000 labeled prompts
fixtures/       Recorded provider responses used by tests
```

---

## Contributors

Built as a student team project by:

- [Ayush Kumar](https://github.com/Ayush-o1)
- [Astik](https://github.com/Astik01)
- [Anubhav](https://github.com/Anubhav104401)
- [Aryan Bhat](https://github.com/aryanbhat2109-ctrl)

---

## License

MIT — see [LICENSE](LICENSE).
