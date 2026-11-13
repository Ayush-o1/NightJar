# Learning guide

A guided tour of NightJar for someone who knows Node/Express and databases and wants to understand this system well enough to explain and defend it. Everything here is in the code; file names are given so you can read along. Where something is *not* built, it says so.

Suggested order: read [REQUEST_LIFECYCLE](architecture/REQUEST_LIFECYCLE.md) once, then this guide with `app/main.py` open.

---

## 1. The big picture

NightJar is a **reverse proxy with opinions**. Your app sends a normal OpenAI-style request to NightJar; NightJar decides whether it can answer from a cache, which model should serve it, and how to call the provider safely; it records what happened.

```
client → [limits, auth] → [route] → [cache] → (miss) → [compress] → LiteLLM → provider
                                      ↑ hit returns here                 ↓
                                  telemetry ←───────────────────── response
```

It is **one Python process plus Redis** (a modular monolith). If you know Express: `app/main.py` is the router file; `app/asgi.py`, `app/middleware.py` are middleware; `app/auth.py` is an auth middleware/dependency; the rest are services called from the route handler.

**Why one process?** Everything on the hot path is either an in-process function call or a network call that would exist anyway (Redis, the provider). Splitting the router or cache into services would add a network hop to the component whose job is to save time. The audit found no problem that a process boundary would fix. See [architecture/SYSTEM_DESIGN](architecture/SYSTEM_DESIGN.md).

**FastAPI vs. Express.** FastAPI handlers are `async def` running on an event loop, like Node. The same rule applies: never block the loop. A CPU-bound call inside a handler stalls every other request. That is not hypothetical here: a single 10 MB request froze the original gateway for over four minutes (section 7).

## 2. Request lifecycle, in code

`chat_completions` in `app/main.py`, in order:

1. **`RequestContextMiddleware`** (`app/asgi.py`): runs before FastAPI. Assigns `X-Request-ID`, rejects bodies over `MAX_REQUEST_BYTES` *before reading them*, and counts bytes for chunked uploads. Catches anything unhandled and returns a JSON 500 that still has the request ID.
2. **Dependencies** (`require_scope("chat")` in `app/auth.py`): authenticate, check scope. Returns a `Principal`.
3. **Validation** (`app/models.py`): pydantic checks roles, ranges, content shape. Failures become OpenAI-shaped 400s (`app/errors.py`).
4. **Text budget** (`total_text_chars`): rejects absurdly long text before anything expensive runs.
5. **Rate limit and model policy**: per-key token bucket (`app/ratelimit.py`), allow-lists.
6. **Routing** (`app/router.py`): picks the model; usually the one you asked for.
7. **Fingerprint** (`app/fingerprint.py`) of the *original* request, then **cache lookup** (`app/cache.py`).
8. **Single-flight**: if an identical request is already running, wait for it.
9. **Compress** (`app/compressor.py`), **call the provider** (`app/proxy.py`), **store** the answer.
10. **Telemetry** (`app/middleware.py`): one row when the response finishes.

Streaming takes a different path (`_stream_response`): see section 7.

**Interview point:** "The order matters. Compression runs after the cache lookup so the cache key reflects what the caller sent, not a non-deterministic summary of it."

## 3. Database design

Two stores, each chosen for what it is good at.

- **SQLite** (`app/telemetry.py`, `app/keys.py`): `telemetry` (one row per request), `request_log` (written by LiteLLM's callback), `api_keys` (hashed keys). Single node, one small write per request, a few dashboard reads. WAL mode.
- **Redis**: cached response payloads with a TTL (`nj:v2:r:<hash>`).

Why SQLite is still fine, and exactly when Postgres would not be: [architecture/DATABASE_DESIGN](architecture/DATABASE_DESIGN.md).

**Migrations** (`_migrate` in `app/telemetry.py`): `PRAGMA user_version` holds the schema version; forward-only migrations *add* columns, each guarded by a `PRAGMA table_info` check so a half-finished run can be repeated. Nothing is dropped. This was tested against a database written by the real 1.x code.

**A subtle design choice:** unknown token counts are stored as `NULL`, not `0`. A stream whose provider sent no usage is *unknown*, not *free*; `SUM` ignores NULL, a `0` would silently deflate cost.

## 4. Caching strategy (the most important part)

**The problem:** an LLM cache can return a wrong answer. A false hit is worse than a miss.

**What was wrong originally** (measured; see [benchmarks/CACHE_CORRECTNESS](benchmarks/CACHE_CORRECTNESS.md)): similarity search over message text only, with no model or parameter in the key. And the embedding model scored "convert 10 miles to km" vs "convert 10 km to miles" at 0.992, *higher* than a genuine paraphrase (0.86). So no threshold can separate them.

**What it does now:**

| Tier | Key | Safe? |
|---|---|---|
| **Exact** (always on) | SHA-256 of a canonical fingerprint: model, every generation parameter, all messages (`app/fingerprint.py`). Unknown request fields are included. | Yes for identical requests |
| **Semantic** (opt-in) | Search only inside the same *partition* (same model, params, conversation; only the last user message differs), similarity ≥ threshold, then **guards** reject disagreement on numbers, negation, quotes, proper nouns (`app/guards.py`) | No: lowercase word swaps still slip through. Measured: ~7% of should-not-match pairs, with a wide uncertainty range |

Other correctness rules (`app/fingerprint.py`): only store complete, plain-text, single-choice answers; never cache tool-using requests or high-temperature ones.

**Concurrency:** *single-flight* (`begin_flight` in `app/cache.py`): when 8 identical requests arrive together, one calls the provider and 7 wait for its result (tested: 1 provider call). If the leader fails, followers get the same error rather than each retrying (a retry storm on a failing provider).

**Alternatives you should be able to discuss:** tune the threshold (measured: doesn't work); cross-encoder/LLM verification of each hit (would catch the swaps, but costs an inference per lookup, i.e. the thing the cache saves); per-entry learned thresholds (vCache paper; needs correctness feedback this system doesn't have); drop semantic entirely (rejected: useful for FAQ-style traffic if the operator chooses it knowingly). ADR-008.

**Why numpy instead of FAISS** (ADR-009): benchmarked: equal speed at 10k–100k vectors, and a numpy matrix makes partition filtering, deletes and per-entry expiry trivial.

## 5. Routing strategy

`app/router.py` answers two separate questions: *classify* (simple/complex from token count and keywords in `config/routing_rules.yaml`) and *apply* (do we actually switch models?). Classification always runs. Application is controlled by `ROUTING_MODE`: by default your model is honoured, and routing happens only for the alias `nightjar/auto`.

Why: silently swapping the model changes behaviour the caller did not ask for. And because classification is always recorded (`not_applied(explicit):...`), you can see what routing *would* save before enabling it.

**Honest limits:** the router has no quality signal. 92.8% agreement with labels on 1,000 short prompts says the rules match their labels, not that cheaper models answer well. A learned router (RouteLLM) needs preference data this project does not have, so it is not built.

Detail worth knowing: complex keywords match word *prefixes* (so `optimiz` catches "optimizing"), simple keywords match *whole words* (so `hi` does not match "which"). That asymmetry is deliberate: sending a hard prompt to a cheap model is the costlier mistake.

## 6. Provider abstraction

NightJar writes **no provider adapters**. `app/proxy.py` wraps the LiteLLM Router: one call shape for OpenAI, Anthropic, Gemini and others, with retries, fallbacks and cooldowns. What NightJar adds is a *policy* around it:

- explicit **timeout** (LiteLLM's default is 6000 s);
- **retry policy**: retry 429/5xx/timeouts, never 401/400 (retrying a 401 only adds latency);
- **cooldown policy**: only server-side errors count toward the circuit breaker (otherwise one bad request took the only deployment offline; reproduced and now a test);
- **error translation** (`app/errors.py:from_upstream`): a provider 401 means *our* key is wrong, so the caller gets a 502, never a 401.

What is *not* done: fallback chains and default model names are still hard-coded in `app/proxy.py` and some names may be out of date.

## 7. Concurrency and async

**The event loop.** One thread runs all handlers; `await` yields to others. CPU-bound work blocks everyone. Three techniques in the code:

1. **Bound the input** (`app/tokens.py`): tiktoken is quadratic on long whitespace-free text (200 KB took 8.6 s). Token counting is exact up to 8,000 characters and estimated beyond, and limits reject huge bodies before any of it runs.
2. **Move blocking calls to a thread** (`asyncio.to_thread`): embedding, SQLite writes, snapshot writes.
3. **Don't make it worse:** the vector search is sub-millisecond at its capped size, so it stays on the loop.

**Pure ASGI middleware** (`app/asgi.py`, `app/middleware.py`) instead of Starlette's `BaseHTTPMiddleware`: it must see the *response stream* and the *request body as it arrives*. (The Starlette docs recommendation could not be fetched while researching; see SOURCE_REFERENCES #12.)

**Streaming** (`_stream_response`): the first chunk is awaited *before* the response is created, so connect-time failures are real HTTP errors. After headers go out the status cannot change, so later failures are in-band `error` events. The upstream stream is always closed in a `finally`; if the client disconnects, the provider call is cancelled (tested over a real socket).

**Backpressure** (`app/limits.py`): a counter of provider calls in flight; beyond `MAX_CONCURRENT_UPSTREAM` the gateway answers 503 + `Retry-After` immediately. A counter, not a queue: a queue just moves the unbounded wait. Cache hits and single-flight followers take no slot.

## 8. Reliability mechanisms

Summarised in one table in [architecture/FAILURE_HANDLING](architecture/FAILURE_HANDLING.md), each marked *tested* or not. The ones to be able to explain: bounded retries, error-class-aware cooldown (the circuit breaker), load shedding, graceful degradation (Redis or embedder down ⇒ miss, not 500), cancellation on disconnect, single-flight error sharing. And the one that is *missing*: a gateway-level **retry budget**.

## 9. Observability

- **Request ID** on every response, stored in `telemetry.request_id`, included in 500 messages: one value links a caller's report, a log line and a database row.
- **Structured logs** (structlog). Verified: no log event contains prompt content.
- **Telemetry** (`app/telemetry.py`): model requested/used, routing decision, cache tier, tokens, estimated cost, latency (to the *end* of a stream), HTTP status, error code, key name.
- **Failures are visible:** a failed telemetry write is counted and shown in `/health`; before, it vanished.
- **Dashboard** reads the API only; it shows nothing it didn't get from a response.

Cost is an **estimate** from a short static table; `request_log.total_cost` (LiteLLM's pricing) is better where it exists. Two tables remain; they cannot be joined (documented).

## 10. Performance considerations

Measured, with limits stated, in [benchmarks/GATEWAY_OVERHEAD](benchmarks/GATEWAY_OVERHEAD.md): miss overhead ≈ 14.5 ms on a 50 ms fake provider (0.7 ms NightJar, 6.5 ms LiteLLM, ~7 ms not attributed); exact hit ≈ 0.5 ms; semantic hit ≈ 5 ms.

The method matters as much as the numbers: profile before optimizing; one optimization came out of it (a semantic miss embedded the prompt twice; fixing it cut c=1 miss from 91.9 to 80.2 ms). And the c=64 rows were *discarded* because the load generator saturated; an honest benchmark reports what it cannot say.

## 11. Security

[architecture/SECURITY_MODEL](architecture/SECURITY_MODEL.md) is a threat table. Core ideas:

- **Keys** (`app/keys.py`): 256 random bits, only a SHA-256 stored, shown once. A fast hash is correct *for high-entropy keys* (it would be wrong for passwords: there, a slow salted hash resists guessing).
- **Scopes** (`chat`, `admin`), per-key model allow-list and rate limit.
- **Auth modes**: `auto` (open until a key exists), `required` (Docker), `off`. A good example of a default chosen deliberately: local convenience without making containers insecure.
- **Bounded everything** (OWASP LLM10 "denial of wallet"): body, text, tokenization, concurrency, per-key rate.
- **Don't leak provider secrets**: fixed messages for server-side errors; redaction + truncation for the rest.
- **Known gaps** (stated, not hidden): cache is shared across callers (no per-key partition), no spend limits, per-process rate limits.

## 12. Deployment

`Dockerfile`: multi-stage; CPU-only PyTorch (the default Linux wheel pulls CUDA libraries); optional model pre-download; non-root user; `NIGHTJAR_AUTH=required`; health check. `docker-compose.yml`: Redis is not published to the host and `REDIS_URL` is overridden to the service name (inside a container `localhost` is the container itself, which would have silently disabled the cache). Details and the verification record: [operations/DEPLOYMENT](operations/DEPLOYMENT.md). CI (`.github/workflows/ci.yml`) runs lint, the suite against a Redis service container, a lockfile-sync check and a smoke benchmark.

---

## Things the process taught (worth retelling)

1. **Verify the claim, not the code.** The README said "~88% routing accuracy"; the number came from a seeded random generator in a `--dry-run` mode. The real figure was 92.8%, on a dataset that could not support the claim.
2. **Mocks hide integration bugs.** 175 passing tests mocked the whole pipeline. Real Redis, a fake provider over HTTP, and live runs found: a setting whose env var name differed from the docs (twice), test pollution that caused real outbound calls with fake keys, and a compose file that silently disabled the cache.
3. **A benchmark is a claim; state its limits.** Single machine, one run, fake provider, saturated load generator.
4. **Make the failure visible.** A swallowed exception is a bug you will meet in production.

## Where to read next

`app/main.py` → `app/cache.py` → `app/fingerprint.py` → `app/proxy.py` → `tests/test_cache_pipeline.py` and `tests/test_reliability.py` (they read like specifications) → [DECISIONS](DECISIONS.md).
