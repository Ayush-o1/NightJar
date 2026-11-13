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

1. **Model routing (opt-in)** — classifies each prompt as `simple` or `complex` from token count and keyword rules (`config/routing_rules.yaml`) and can switch to the matching model tier (e.g. `gpt-3.5-turbo` vs `gpt-4o`). By default NightJar uses the model you asked for and only routes when you request the alias `nightjar/auto` (or set `ROUTING_MODE=always`). The tier it *would* have chosen is recorded either way, so you can see the effect before enabling it. On the repo's 1,000-prompt labeled set the classifier agrees with the labels 92.8% of the time; that set is short, keyword-aligned prompts, so it shows the rules match their labels, not that cheaper models answer well. Details: [docs/BASELINE.md](docs/BASELINE.md).

2. **Response caching** — an **exact tier** (always on) returns the stored answer when the same model, parameters and messages were already answered; identical requests arriving together share one provider call. An optional **semantic tier** (`SEMANTIC_CACHE=true`, off by default) also serves near-duplicate prompts from the same conversation context, using local embeddings (`all-MiniLM-L6-v2`) and lexical guards. Semantic matching can return a wrong answer: on the repo's labeled pairs it served 3 of 41 should-not-match pairs even with the guards (about 7%, plausible range roughly 2–20%), because the embedding model scores some opposite-meaning prompts higher than true paraphrases. Details and method: [docs/benchmarks/CACHE_CORRECTNESS.md](docs/benchmarks/CACHE_CORRECTNESS.md).

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
  Limits + auth ──── body size, text budget, validation, API key
     │
     ▼
  Route ──── keep requested model (default) or pick a tier for "nightjar/auto"
     │
     ▼
  Cache ──── exact lookup → (opt-in) guarded semantic lookup → single-flight
     │
 HIT ┴ MISS ──→ Compress (long chats) ──→ LiteLLM Router ──→ Provider API
     │                                         │
     │                                     Cache store
     └──────────────┬──────────────────────────┘
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
| Vector search | numpy brute-force cosine, scoped per cache partition (ADR-009) |
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

Responses carry diagnostic headers: `X-Cache` (HIT/MISS/BYPASS), `X-Cache-Tier` (exact/semantic/coalesced), `X-Cache-Reason` (why a request bypassed the cache), `X-Similarity`, `X-Model-Tier`, `X-Model-Selected`, `X-Compressed`, `X-Compression-Ratio`.

Request headers: `X-NightJar-Model-Override` (force a specific model), `X-NightJar-No-Compress: true`, and `X-Request-ID` (your own trace id, echoed back). Every response carries `X-Request-ID`. The old `X-ContextForge-*` header names still work but are deprecated.

---

## Configuration

Every variable is documented inline in [`.env.example`](.env.example). The ones you're most likely to touch:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` etc. | `""` | Provider keys — set at least one (`ANTHROPIC_`, `GEMINI_`, `GROQ_`, `MISTRAL_`, `COHERE_`, `XAI_`) |
| `SIMPLE_MODEL` | `gpt-3.5-turbo` | Model for the simple tier |
| `COMPLEX_MODEL` | `gpt-4o` | Model for the complex tier |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `SIMILARITY_THRESHOLD` | `0.92` | Cosine similarity required for a semantic hit (only with `SEMANTIC_CACHE=true`) |
| `CACHE_TTL_SECONDS` | `86400` | Cached response lifetime |
| `COMPRESS_THRESHOLD` | `2000` | Token count that triggers compression |
| `COMPRESS_MIN_TURNS` | `6` | Minimum turns before compression applies |
| `SEMANTIC_CACHE` | `false` | Enable the semantic tier (see correctness report) |
| `CACHE_MAX_TEMPERATURE` | `1.0` | Requests above this temperature are never cached |
| `NIGHTJAR_AUTH` | `auto` | `auto`: enforce once any key exists; `required`: always; `off`: never |
| `NIGHTJAR_API_KEYS` | `""` | Comma-separated full-access bearer tokens (simple option; prefer `python -m app.keys`) |
| `CORS_ALLOW_ORIGINS` | `""` | Allowed browser origins (default none; was `*` before 2.0). Credentialed CORS is never enabled |
| `ROUTING_MODE` | `explicit` | `explicit`: honour the requested model, route only for `nightjar/auto`; `always`: route every request; `off`: never switch |
| `ALLOWED_MODELS` | `""` | Comma-separated models callers may request/override to. Empty = any |
| `MAX_REQUEST_BYTES` | `8388608` | Request body limit (`413` above it) |
| `MAX_PROMPT_CHARS` | `2000000` | Text-length limit across all messages (`400` above it) |
| `TEST_MODE` | `false` | Force the cheapest model for every request |

---

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions |
| `GET` | `/health` | Liveness + Redis reachability |
| `GET` | `/v1/telemetry` | Paginated request records |
| `GET` | `/v1/telemetry/summary` | Aggregated stats |
| `GET/POST` | `/v1/threshold*` | Removed in 2.0 (answers `410`) |
| `GET` | `/v1/cache/stats` | Vector count, Redis keys, hit/miss counters |
| `DELETE` | `/v1/cache` | Flush the cache |
| `DELETE` | `/v1/cache/{key}` | Invalidate one entry |
| `GET` | `/admin/usage` | Spend/token summary (filterable) |
| `GET` | `/admin/logs` | Raw request log |
| `GET` | `/admin/savings` | Estimated savings from cache + routing |

Schemas and examples: [docs/API.md](docs/API.md).

**Auth:** with no keys configured a local install is open (convenient on localhost); as soon as any key exists, every endpoint except `/health` requires `Authorization: Bearer <key>`. Create scoped keys with `uv run python -m app.keys create --name app --scopes chat --rpm 60`. The Docker image runs with `NIGHTJAR_AUTH=required`, so a container with no keys refuses requests instead of serving them to anyone. Details: [docs/architecture/SECURITY_MODEL.md](docs/architecture/SECURITY_MODEL.md).

---

## Dashboard

Open `http://localhost:8000/dashboard/` while the backend is running.

Shows the request log, cache hit rate, model and tier distribution, latency and cost trends, routing-reason breakdown, — all computed from real `/v1/*` responses. If it can't reach a backend it falls back to a demo dataset and says so in the header badge.

It's plain HTML, CSS and JavaScript with Chart.js — no build step, no framework. Tables collapse into stacked records on narrow screens, colour is only used where it means something (a cache hit saved a call; a slow request needs a look), and every screen has a real empty state instead of a blank panel.

---

## Testing

```bash
uv run ruff check app/ tests/ benchmarks/
PYTHONPATH=. uv run pytest tests/     # mocked — no network, no Redis needed
```

This is exactly what CI runs. The suite covers the router classifier (including accuracy against the labeled dataset), cache correctness against a real Redis (exact and semantic tiers, single-flight, Redis and embedder outages), compression triggers and fallback, telemetry read/write, auth on/off, failover, tool-call handling, and the proxy's error propagation.

**Live E2E tests** (`tests/test_e2e.py`) are marked `e2e` and excluded by default — they hit a real provider and cost real money:

```bash
RUN_E2E_TESTS=1 OPENAI_API_KEY=sk-real-key PYTHONPATH=. pytest tests/test_e2e.py -m e2e
```

**Benchmarks** (see [benchmarks/README.md](benchmarks/README.md)): `benchmarks/gateway_bench.py` measures gateway overhead and cache-hit latency against a local fake provider (no API key needed); `benchmarks/cache_eval.py` measures the semantic cache's false-hit rate. Results, with their limits, are in [docs/benchmarks/](docs/benchmarks/).

```bash
uv run python benchmarks/gateway_bench.py --smoke     # ~10 s, needs redis-server on PATH
uv run python benchmarks/cache_eval.py                # loads the embedding model
```

---

## Implementation notes

**Two telemetry tables, on purpose.** `telemetry` gets a row for every request including cache hits, with estimated costs. `request_log` is written by LiteLLM's success callback only when a provider was actually called, and carries real cost from `litellm.completion_cost()`. `telemetry` answers "what did the gateway do", `request_log` answers "what did we pay for". `/admin/usage` and `/admin/savings` read the latter.

**Redis is optional at runtime.** Cache reads and writes catch connection errors and degrade to a miss, so a Redis outage makes the gateway slower, not broken. `/health` reports Redis separately from process liveness.

**The vector index is snapshotted atomically.** All index operations take one lock; a background task writes `./data/vectors.npz` (temp file + rename) when it changed and at shutdown, so a crash can't leave a torn file. Expired entries are dropped on a timer and on load.

**Streaming skips the cache.** You can't match a token stream against a cache before it exists, and buffering it to compress would defeat streaming. Streamed requests are still routed and still logged.

**The dashboard escapes everything.** `model_used` is attacker-controllable through the `X-NightJar-Model-Override` header, so every dynamic value is escaped before it reaches `innerHTML`.

---

## Troubleshooting

**Startup hangs ~15s with no internet** — sentence-transformers pings Hugging Face even when the model is cached. Skip it with `HF_HUB_OFFLINE=1`.

**`no such table` or missing telemetry columns** — the schema is created at startup, so an old `./data/telemetry.db` from before a schema change won't have new columns. Delete it and restart; it's local runtime data, not source.

**`ModuleNotFoundError: No module named 'app'`** — run pytest with `PYTHONPATH=.`.

**Cache lookups behaving oddly** — `DELETE /v1/cache` flushes Redis and the vector index; deleting `data/vectors.npz` while stopped also resets the index. Old `data/faiss.index*` files from 1.x are no longer used and can be deleted.

---

## Known limitations

- **Single instance.** The vector index and SQLite database are local files, and single-flight is in-process, so those don't span replicas (cached responses in Redis do). Scaling out means a shared vector store and Postgres; see ADR-003 and ADR-009 in [docs/DECISIONS.md](docs/DECISIONS.md).
- **Routing quality is not measured.** The 92.8% figure is agreement with hand-written labels on short prompts, not evidence that a cheaper model answered adequately. Live traffic has no ground truth.
- **Compression costs a call.** Summarizing older turns is itself an LLM request, so it only pays off on genuinely long conversations — hence the token *and* turn-count gates.
- **Costs in `telemetry` are estimates** from a static per-model table that goes stale as providers change pricing. `request_log` has the real numbers.
- **One similarity threshold** for the semantic tier, not per-model or per-tenant, and the tier cannot be made fully safe (see the correctness report).
- **Limited access control.** Scoped keys, per-key model lists and request-rate limits exist; per-key spend limits, cache tenant isolation and a shared (multi-process) rate limiter do not. See the security model.

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
