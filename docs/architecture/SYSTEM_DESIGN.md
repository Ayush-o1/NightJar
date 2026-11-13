# System design

NightJar is one Python process (a modular monolith) plus Redis. This page describes what the code does **now**; planned changes are in [../ROADMAP.md](../ROADMAP.md) and the phase notes in [../IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md). Last updated for Phase 3.

```mermaid
flowchart LR
    App[Client app<br/>OpenAI SDK] -->|HTTP, Bearer key| MW

    subgraph NightJar process
      MW[RequestContext middleware<br/>request id, body limit, 500 net] --> CORS[CORS] --> TEL[Telemetry middleware]
      TEL --> API[FastAPI routes<br/>app/main.py]
      API --> AUTH[auth dependency]
      API --> ROUTER[ModelRouter<br/>rules + token estimate]
      API --> COMP[Compressor]
      API --> CACHE[SemanticCache]
      CACHE --> EMB[Embedder<br/>MiniLM, thread]
      CACHE --> FAISS[(FAISS<br/>in-process)]
      API --> PROXY[ProxyClient<br/>LiteLLM Router]
      TEL --> SQLITE[(SQLite<br/>telemetry.db)]
    end

    CACHE --> REDIS[(Redis<br/>response payloads, TTL)]
    PROXY --> P1[OpenAI]
    PROXY --> P2[Anthropic]
    PROXY --> P3[Gemini / Groq / ...]
```

## Components

| Module | Responsibility | State |
|---|---|---|
| `app/asgi.py` | Request IDs, body-size limit, last-resort JSON 500 | none |
| `app/errors.py` | Error type, OpenAI-shaped bodies, provider-error translation, secret redaction | none |
| `app/main.py` | Routes, request pipeline, SSE handling, lifespan wiring | `app.state` holds the long-lived objects |
| `app/auth.py` | Bearer-token check (flat key list) | none |
| `app/models.py` | Request validation (roles, ranges, multimodal content) | none |
| `app/tokens.py` | Message-text extraction; bounded token counting | encoder cache |
| `app/router.py` | Complexity classification, tier → model | YAML rules loaded at start |
| `app/compressor.py` | Summarizes old turns past a token threshold | none |
| `app/cache.py`, `embedder.py`, `vector_store.py` | Semantic cache: embed, FAISS search, Redis fetch | FAISS file + Redis |
| `app/proxy.py` | Provider calls via LiteLLM Router, retries/fallbacks | Router in memory |
| `app/telemetry.py`, `middleware.py` | Per-request rows | SQLite |
| `app/adaptive.py` | Threshold controller (to be removed in Phase 5) | SQLite |

## Why a monolith

Everything on the hot path is either an in-process call (router, tokens, FAISS) or a network call that would exist anyway (Redis, providers). Splitting the router, cache or embedder into services would put a network hop in front of the thing whose job is to save time, and the audit found no problem that a process boundary would fix. The first measured reason to split would be embedding saturating a core; the design keeps embedding behind `asyncio.to_thread` so it can move to a worker pool and then to a separate process if [Phase 11](../ROADMAP.md) shows that.

## State and what limits scaling out

| State | Where | Shared across replicas? |
|---|---|---|
| Response payloads | Redis | Yes |
| Vectors + id map | FAISS file on local disk | **No** |
| Telemetry, threshold history | SQLite file on local disk | **No** |
| LiteLLM cooldown/failure counters | In memory | **No** |

So today a second replica has its own cache index and its own telemetry. Details and upgrade paths: [SCALABILITY.md](SCALABILITY.md) (written in Phase 11/13 with measurements).

## Process-level concurrency

One uvicorn worker, one event loop. Handlers are `async`; CPU-bound or blocking calls are pushed to threads (`asyncio.to_thread`): embedding and the telemetry write. Not yet off the loop: FAISS search/add and the other SQLite calls (tracked in the audit, D-1 and C-5). Token counting is bounded instead of offloaded (see [../research/TECHNICAL_FINDINGS.md](../research/TECHNICAL_FINDINGS.md) F4).
