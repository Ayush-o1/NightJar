# Technical audit

Audit of the code at `1a4819c` (before modernization), read in full: about 2,500 lines under `app/`, plus tests, Docker, CI, benchmarks, dashboard and docs. Measurements behind these findings are in [BASELINE.md](BASELINE.md). Severity reflects impact on a gateway that someone might actually deploy: **Critical** (wrong answers, outage, or data exposure), **High**, **Medium**, **Low**.

Each finding lists the file so it can be checked, and the phase of [ROADMAP.md](ROADMAP.md) that addresses it.

## 1. What the application actually is

One FastAPI process. Request path in `app/main.py:chat_completions`:

```
route (router.py)  →  [stream? forward and return]  →  compress (compressor.py)
   →  cache.lookup (embed → FAISS top-1 → Redis GET)  →  hit: return
   →  miss: LiteLLM Router.acompletion  →  cache.store  →  return
```

`TelemetryMiddleware` writes one row to table `telemetry` after the response. LiteLLM's success callback writes one row to `request_log`. SQLite, FAISS and the embedding model all live in the one process.

The architecture is a reasonable shape for a single-instance gateway, and it doesn't need microservices. The problems are in correctness and robustness of the individual stages, not in the overall layout.

## 2. Capabilities claimed vs. verified

| README / docs claim | Verdict |
|---|---|
| OpenAI-compatible chat completions | **Partly.** No multimodal `content` (list parts → 422), error bodies don't use OpenAI's `error` object, empty `messages` accepted, `tools` pass through only via `extra="allow"`. |
| Rule-based routing, "~88% accuracy" | Routing works. **88% is not a measurement** (random constant in `--dry-run`); real figure on the labeled set is 92.8%, and the set can't validate the routing idea (see B-routing below). |
| Semantic caching | Works mechanically. **Not safe**: see C-1 to C-4. |
| Redis-backed response storage | Works; degrades to miss on Redis errors (tested). |
| Context compression | Works for plain text. Breaks tool-call conversations (C-8). |
| Multi-provider via LiteLLM | Works. Deployment list and fallbacks are hard-coded in `proxy.py` (old model names). |
| "Failover chains … enforced from `routing_rules.yaml`" | **False.** `fallbacks:` and `provider_preference:` in the YAML are never read. The real chains are hard-coded in `ProxyClient._build_fallback_map` and use different model names. |
| Request telemetry | Works. Streaming requests record zero tokens and zero cost. Failed requests are not in `request_log` (`status` values `cached` and `error` are documented but never written). |
| Adaptive threshold | **Runs only when someone POSTs `/v1/threshold/evaluate`.** Nothing calls it on a schedule. The logic is unsound (see C-3). |
| Cache invalidation | Works for single key and flush. Entries expire from Redis by TTL but their vectors never leave FAISS (C-5). |
| Cost and token tracking | `request_log` costs (from LiteLLM) are real. `telemetry` costs use a stale 5-model table. Cache savings always `$0` (zero tokens recorded on hits). |
| Admin APIs | Work. Unauthenticated by default. |
| Dashboard | Real data when the backend is reachable; labeled demo data otherwise. |
| SQLModel / SQLAlchemy (README, ADR-003) | **Not used.** Zero imports; telemetry is raw `sqlite3`. Both are in `requirements.txt`. |
| `OPENAI_BASE_URL` setting | **Unused** by any code. |
| Docker deployment | Dockerfile and compose look sound on reading. **Not built in this baseline.** |
| Automated testing | 175 mocked tests pass. Integration gap noted in BASELINE. |

## 3. Findings

### Critical

**A-1. A single large request stalls the whole gateway.** `router.py:count_tokens` runs `tiktoken.encode` on the full prompt, synchronously, inside an `async def` handler. Encode time is quadratic on long whitespace-free input (200 KB → 8.6 s; 10 MB → pinned for >4 min, `/health` unresponsive). No body size limit exists. With auth off by default, anyone who can reach the port can take it down. *Phase 3, 9.*

**C-1. Cache key ignores everything except message text.** `main.py:244` calls `cache.lookup(body.model, messages, threshold=…)`. The model name is passed but never used in the search; FAISS is searched globally. Temperature, `max_tokens`, `n`, `tools`, `response_format`, `stop`, and `user` don't affect lookup. A response generated for `gpt-4o` at temperature 1.2 is served to a `claude` request at temperature 0. *Phase 5.*

**C-2. Similarity alone cannot tell "same question" from "opposite question".** Measured with the shipped embedder: miles→km vs km→miles 0.992, ascending vs descending 0.951, 2022 vs 2023 0.944; a true paraphrase scored 0.86. There is no threshold that fixes this. The default 0.92 serves wrong answers. *Phase 5.*

**C-3. Adaptive threshold optimizes the wrong thing.** It lowers the threshold when hit rate < 20% (floor 0.70) and raises it when > 60%. Hit rate is not a quality signal: a lower threshold raises the hit rate by admitting wrong answers, and the controller treats that as success. Nothing measures whether a hit was correct. At 0.70 it accepts "reset my password" for "reset my username". It also changes behavior only when manually triggered. *Phase 5 (remove or replace).*

**S-1. Admin and cache-flush endpoints are open by default.** `require_api_key` is a no-op when `CONTEXTFORGE_API_KEYS` is empty, which is the default. Anonymous `DELETE /v1/cache` and `GET /admin/*` worked in the baseline. Compose publishes both the app (8000) and Redis (6379, no password) to the host. *Phase 9, 13.*

### High

**R-1. The router discards the model the client asked for.** `route()` ignores `model_requested` for selection. Asking for `gpt-4o` with the prompt "hi" gets `gpt-3.5-turbo`. For a drop-in OpenAI-compatible endpoint, silently substituting a model is a behavior change that many callers would not expect, and it happens on every request unless an override header is sent. There is no opt-in. *Phase 4.*

**R-2. Routing evidence is circular.** The labeled set is 1,000 short prompts whose labels line up with the router's keywords; no prompt reaches the token rules. The metric measures the keyword list against itself. Substring matching also produces wrong explanations (`hi` matches "Which", `no` matches "know", `ok` matches "token": reported as `simple_keyword:hi`). The simple-keyword branch is also redundant: it returns SIMPLE only if tokens ≤ `simple_max_tokens`, and the next rule returns SIMPLE under the same condition. *Phase 4.*

**E-1. Error handling is incomplete.** Only `UpstreamError` is caught in the handler. Any other exception → plain-text `500`. In streaming, `UpstreamError` is raised inside the generator after the `200` and headers are sent, so clients get a truncated stream and the server logs a traceback. Validation errors use FastAPI's `detail` shape instead of OpenAI's `error` object, which breaks SDK error parsing. *Phase 3.*

**E-2. Streaming is not observed.** `TelemetryMiddleware` (a `BaseHTTPMiddleware`) records latency at the moment headers return, not when the stream ends; tokens and cost are zero. Starlette's docs list known limitations of `BaseHTTPMiddleware` and recommend pure ASGI for streaming. *Phase 3, 8.*

**C-4. Compression runs before the cache and changes the cache key.** The cache key is built from the compressed messages, whose summary text is an LLM output and varies between calls, so long conversations will rarely hit the cache. The compressor is also invoked on every long request with no memoization, so the summarization call can cost more than it saves. *Phase 6.*

**C-5. FAISS never forgets.** Redis entries expire after `CACHE_TTL_SECONDS`; their vectors stay forever. Lookups for expired entries return a nearest-neighbor "hit" that then fails the Redis `GET` and is counted as a miss; the index only grows. `remove_by_key` rebuilds the whole index (O(n)) under the lock. Persistence only happens on clean shutdown, so a crash loses the index while Redis keeps its keys (and vice versa). *Phase 5.*

**C-6. Cache stores before it checks response eligibility.** Every non-stream response is stored, including `tool_calls` responses, `n>1`, and non-`stop` finish reasons such as `length`. *Phase 5.*

**C-7. Concurrent identical requests all go upstream.** No single-flight. N simultaneous identical prompts → N paid calls. *Phase 5.*

**C-8. Compression can corrupt tool conversations.** `_split_messages` cuts at a fixed count, so an assistant message with `tool_calls` can be summarized away while its `role=tool` replies stay (providers reject this), and `count_tokens` ignores `tool_calls` content. The summary is inserted with `role="user"`. *Phase 6.*

**S-2. Client-controlled identity.** `user_id` in the logs comes from the request's `user` field, so any caller can attribute spend to anyone. Auth is a flat list of shared tokens with no key identity, so cost can't be attributed per key. *Phase 9.*

**S-3. `X-ContextForge-Model-Override` lets any caller pick any model**, including expensive ones, with no allow-list. The README acknowledges the dashboard-injection angle but not the cost angle. *Phase 4, 9.*

**S-4. Logging and data handling.** The proxy does not log prompts (good), but failed upstream errors are returned verbatim (`detail=str(exc)`), which can include provider messages containing account details or key fragments. There is no body/field size cap and no PII guidance. *Phase 3, 9.*

**D-1. Telemetry writes silently fail.** `write_record` and `write_request_log` have `except Exception: pass`. A full disk or locked database drops data without a trace. Each write opens a new SQLite connection and runs synchronously on the event loop, behind a process-wide lock. `DB_PATH` is captured at import time, so settings can't change it later. *Phase 8.*

**D-2. Two telemetry tables, two cost methods, no join key.** `telemetry.request_id` is a fresh UUID; `request_log.request_id` is LiteLLM's call id. A request can't be traced across both. Cost per request is computed twice with different methods (static table vs. `litellm.completion_cost`). `/admin/savings` assumes a $5/$15-per-1M gpt-4o baseline (hard-coded, and stale) for every token. *Phase 8.*

### Medium

- **Dependencies:** `litellm>=1.30.0` unpinned (BASELINE); `sqlmodel`, `sqlalchemy` unused; dev tools (`pytest`, `ruff`) installed into the runtime image; OpenTelemetry packages always installed though opt-in. No lockfile. *Phase 13.*
- **Model names are stale.** `gpt-3.5-turbo`, `claude-3-5-sonnet-20241022`, `llama3-8b-8192`, `gemini-1.5-*` and `gemini-2.5-pro-preview-03-25` are hard-coded in `proxy.py` and the YAML. Some of these are old enough that they may have been retired or superseded by the providers; I have not checked each one against current provider docs. Either way, model lists belong in config, not code. *Phase 4.*
- **Blocking work on the event loop:** embedding (`SentenceTransformer.encode`), FAISS search/add, tiktoken, and all SQLite calls run synchronously in async handlers. Throughput is bounded by a single core, and one slow call blocks every client. *Phase 3, 11.*
- **`forward_with_tools` is dead code**, referenced only by tests; the "translation guard" never runs in the real request path.
- **FAISS read/write race:** `search` takes no lock while `add` and `remove_by_key` mutate the index. `remove_by_key` also rebinds `self._index`. The README claims writes are locked and that is true, but reads are not covered. *Phase 5.*
- **`/v1/telemetry?limit=` has no upper bound**; the dashboard asks for 500 and a caller can ask for everything. *Phase 8.*
- **No request IDs** propagate to responses or logs, so a user can't correlate a failed call with a log line. *Phase 3.*
- **Config sprawl:** two Redis settings (`redis_url` plus `redis_host/port/password`) that can disagree; `enable_cache` controls a second, exact-match LiteLLM cache that is off by default and is a different feature from the semantic cache. *Phase 2.*
- **CORS is `*` by default**, plus an open admin API. With auth off, any web page in a browser on the same machine can call the gateway.
- **Tests write to the real `./data` directory**, share one module-level FastAPI app, and none start Redis. *Phase 12.*
- **Dashboard lives in `docs/dashboard/`** and is shipped in the image from there; it is application code, not documentation. It carries a hard-coded demo dataset of 24 invented requests. It is labeled as demo data, which is honest, but it still puts fabricated analytics in the repo. *Phase 10.*
- **Docker:** compose publishes Redis to the host without a password; the app container runs a single uvicorn worker; `.dockerignore` excludes `tests/` and `benchmarks/` (fine) but the image carries pytest and ruff. *Phase 13.*
- **CI:** one job; runs `--dry-run` benchmark, which only validates its own fake numbers (so the step cannot fail for a real reason); no Redis service, so no integration tests; the workflow history shows 24 tests failing on every run "for months" until ADR-006. *Phase 12.*

### Low

- `compressor.py` uses stdlib `logging` with `%s`-style calls while the rest of the code uses structlog.
- Duplicated token-counting code in `router.py` and `compressor.py`.
- Version `"1.0.0"` hard-coded in four places (`main.py` twice, `models.py`, `pyproject.toml`).
- ADR-003 and README describe SQLModel/SQLAlchemy that is not used; ADR numbering has gaps in dates.
- Old project name in ~30 files (see rename notes in [ROADMAP.md](ROADMAP.md)).

## 4. Duplicate and unused code

| Item | Status |
|---|---|
| `ProxyClient.forward_with_tools` | Unused outside tests |
| `compress_context_with_metadata` metadata | Computed, then dropped by `compress_context`; only the ratio reaches telemetry |
| YAML `fallbacks`, `provider_preference` | Never read |
| `Settings.openai_base_url` | Never read |
| `sqlmodel`, `sqlalchemy` in requirements | Never imported |
| Pydantic response models in `models.py` (`ChatCompletionResponse`, `Choice`, chunk types…) | Defined, only `HealthResponse` and the request model are used |
| `costs.py` `COST_TABLE` vs `litellm.completion_cost` | Two sources of truth for price |
| `status` values `cached`, `error` in `/admin/logs` docs | Never written |

## 5. What is good and should be kept

- Module boundaries are sensible and small. Each file does one thing.
- Redis outage degrades to a miss, covered by tests.
- `hmac.compare_digest` for token comparison; CORS credentials correctly off.
- WAL mode and a timestamp index on telemetry.
- Honest dashboard badge for demo vs. live data; HTML-escaping of dynamic values.
- Non-root container, multi-stage build, model pre-downloaded at build time.
- The decision log (`DECISIONS.md`) exists and is mostly candid. I'll extend it, not replace it.

## 6. Decisions this audit forces

1. **Fix cache correctness before adding any feature.** C-1, C-2 and C-6 are the reason to distrust the whole cache. The fix has to be a different design, not a different threshold (see [research/TECHNICAL_FINDINGS.md](research/TECHNICAL_FINDINGS.md)).
2. **Routing must not override explicit model requests by default.** Opt-in routing through a distinct model alias is the compatible design.
3. **Everything CPU-bound leaves the event loop, and inputs are bounded** before any of it runs.
4. **Retain SQLite, FAISS-in-process and Redis.** None of the audited problems are caused by those choices; they come from how they're used. Replacing them would be churn. Reasoning in [architecture/DATABASE_DESIGN.md](architecture/DATABASE_DESIGN.md).
