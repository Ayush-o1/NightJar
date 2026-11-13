# Roadmap

Derived from [TECHNICAL_AUDIT.md](TECHNICAL_AUDIT.md) and [research/TECHNICAL_FINDINGS.md](research/TECHNICAL_FINDINGS.md). Phases are ordered by dependency and by risk: things that make the gateway wrong or unsafe come before things that make it nicer. Current progress is in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Product scope

> NightJar is a self-hosted, OpenAI-compatible gateway that sits in front of LLM providers and lowers cost and latency **without returning wrong answers**, with a per-request record of what it did and why.

In scope: a safe response cache, opt-in cost routing, context compression, provider failover, per-key access control, request telemetry, a dashboard that reads real data.

Out of scope (decided, with reasons in [research/LANDSCAPE.md](research/LANDSCAPE.md)): provider adapters, learned routing, evals/prompt management, multi-tenant SaaS, content guardrails.

Features that stay because the audit found them sound: Redis-with-graceful-degradation, LiteLLM as provider layer, FAISS in-process, SQLite, vanilla-JS dashboard. Features that change: cache design, adaptive threshold (removed), routing default (opt-in), telemetry (one table), auth (on by default).

## Rename policy: ContextForge → NightJar

| Item | Change | Compatibility |
|---|---|---|
| Docs, README, UI text, titles, logs, package name, Docker names | Renamed | n/a |
| Env var `CONTEXTFORGE_API_KEYS` | `NIGHTJAR_API_KEYS` | Old name still read, with a startup deprecation warning. Removal in the next minor release. |
| Request headers `X-ContextForge-Model-Override`, `X-ContextForge-No-Compress` | `X-NightJar-*` | Old names still accepted, deprecated |
| Response headers (`X-Cache`, `X-Model-Selected`, …) | Unchanged | Not brand-specific, so no break |
| `docs/DECISIONS.md` ADRs 001–006 | Text kept as history; a note is added at the top | Historical record, not rewritten |
| Git history, tags, old GitHub URL | Untouched | GitHub redirects the old URL |

## Phases

Each phase ends with: tests added and passing, `ruff` clean, full suite green, app starts, diff reviewed, docs and status updated, commit(s) pushed. "DoD" below is only what is specific to the phase.

### Phase 0 — Discovery and baseline  ✅
**Problem:** unknown starting condition. **Deliverables:** [BASELINE.md](BASELINE.md), [TECHNICAL_AUDIT.md](TECHNICAL_AUDIT.md). **Done when:** claims in README are checked against code and measured.

### Phase 1 — Product definition and research  ✅
**Problem:** a feature list with no stated purpose. **Deliverables:** `docs/research/*`, this roadmap. **Done when:** every major design choice cites evidence or says it has none.

### Phase 2 — Rename and foundations  ✅
- **Objective:** one name, reproducible installs, one place for config and version.
- **Features:** rename per policy above; `pyproject.toml` becomes the dependency source of truth with a lockfile (`uv.lock`) and pinned litellm; drop unused `sqlmodel`/`sqlalchemy`; dev dependencies split from runtime; single version constant; settings cleaned (remove unused `OPENAI_BASE_URL`; the two Redis settings are reconciled in Phase 5 when the LiteLLM exact cache is replaced).
- **Architecture:** none beyond module layout: `app/` stays flat, new modules arrive with the phase that needs them.
- **API:** header/env aliases above. **DB:** none. **Frontend:** title and text only.
- **Risks:** litellm pin breaks something → run the suite on the pinned version before committing.
- **Acceptance:** `grep -ri contextforge` returns only intentional compatibility code and historical docs; `uv sync` reproduces the environment; deprecated names still work (tested).

### Phase 3 — Core API and request lifecycle  ✅
- **Problem:** audit A-1, E-1, E-2, validation gaps, no request IDs.
- **Features:** `app/errors.py` (error types → OpenAI-shaped bodies); pure-ASGI middleware (request ID, body size limit with 413, timing); OpenAI-shaped error responses for validation, auth, upstream and unexpected errors; `content` accepts string or part lists; `messages` must be non-empty; character budget before tokenization; CPU-bound work off the event loop; streaming errors become in-band SSE `error` events; `X-Request-ID` on every response.
- **API:** error body is `{"error": {"message","type","param","code"}}` everywhere. Validation errors change from FastAPI's `detail` to this shape: **breaking for clients that parse `detail`**; documented.
- **Tests:** 1 MB hostile input returns 413 and `/health` stays responsive; malformed JSON, empty messages, list content, streaming mid-stream failure, unexpected exception.
- **DoD:** the baseline's 10 MB probe is rerun and no longer stalls the server (bounded size reproduced, not the 10 MB case, to avoid hanging the check).

### Phase 4 — Routing and provider layer  ✅ (partly; deployments config moved to Phase 7)
- **Problem:** R-1, R-2, S-3, stale model names, ignored YAML sections.
- **Features:** explicit model honored by default; routing via alias `nightjar/auto` or `ROUTING_MODE=always`; whole-word matching; structured decision (`rule`, `matched`, `tier`, `token_estimate`); tiers, model lists and fallback chains loaded from one validated config file, and the YAML sections that were never read are either read or removed; override header gated by key permission (Phase 9) and an allow-list.
- **Tests:** requested model respected; alias routes; word-boundary cases (`which`, `know`, `token`); config validation errors; fallback chain loaded from file.
- **Acceptance:** README accuracy claim replaced by measured numbers on an extended labeled set with hard cases.

### Phase 5 — Cache correctness  ✅
- **Problem:** C-1…C-3, C-5…C-7.
- **Features:** request fingerprint; exact tier; partitioned, gated semantic tier (opt-in); eligibility rules; TTL-aware vector eviction; timer-based atomic persistence; single-flight for concurrent identical misses; adaptive threshold removed, endpoints return `410 Gone` with a pointer for one release; `scripts/calibrate_threshold.py` for offline calibration; labeled pair set with measured false-hit rate.
- **DB:** none (metadata beside vectors). **API:** `X-Cache: HIT|MISS|BYPASS`, `X-Cache-Tier: exact|semantic`.
- **Risks:** semantic gating gets too strict and hit rate falls. The tradeoff is reported, not hidden.
- **Tests:** against real Redis and real FAISS (integration) plus unit tests; concurrent-miss test asserts one upstream call; the baseline's false-hit pairs are regression tests.
- **Acceptance:** none of the baseline false-hit pairs served at default settings, or the ones that still are (unit-order swaps) are listed in the docs as known limits.

### Phase 6 — Context compression  ✅
- **Problem:** C-4, C-8.
- **Features:** compression runs *after* cache lookup, on the miss path only; never splits an assistant `tool_calls` message from its `tool` replies; token counts include tool calls; summary memoized per conversation prefix; summary clearly delimited; failure falls back to uncompressed; compression off for requests with tools unless opted in.
- **Tests:** tool-call boundary, memoization hit, failure fallback, ratio recorded.
- **Acceptance:** a measured example of tokens before/after with the summarizer stubbed, labeled as such.

### Phase 7 — Reliability  ✅ (retry budget and config-driven fallbacks not done)
- **Problem:** unbounded waits, default cooldown behavior, no retry budget.
- **Features:** per-request timeout and overall deadline; Router `allowed_fails_policy`/`retry_policy` so auth and bad-request errors neither retry nor cool down; retry budget; global concurrency limit with `503` + `Retry-After`; upstream cancellation on client disconnect; error-message mapping (no raw provider text).
- **Tests:** against a **local fake upstream** with scripted latency, 429s, 500s, hangs and malformed JSON: retries bounded, fallbacks used, auth error not retried, saturation sheds load, disconnect cancels.
- **Acceptance:** the baseline's "one 401 puts the only deployment into cooldown" scenario is a test.

### Phase 8 — Database and telemetry  ✅ (table merge, batching not done)
- **Problem:** D-1, D-2, streaming accounting.
- **Features:** single `requests` table; schema versioning with forward-only migrations; migration of `telemetry` and `request_log` data without deleting them; async batched writer with a drop counter; streaming completion records via `include_usage`; cost from LiteLLM price data with `cost_source`; savings figures labeled estimates and computed from real stored token counts; retention job; bounded, indexed query endpoints with cursor pagination.
- **Tests:** migration on a copy of a baseline-format DB, writer under concurrency, queue overflow, streaming record, query bounds.
- **Acceptance:** a request is traceable from its response `X-Request-ID` to one row.

### Phase 9 — Security and access  ✅ (spend limits, cache tenant isolation not done)
- **Problem:** S-1…S-4.
- **Features:** auth on by default; key records (hashed, name, scopes `chat`/`admin`, optional model allow-list, rate limit, daily spend limit) in SQLite; CLI to create/list/revoke keys; per-key rate limiting (token bucket in-process, Redis-backed when available); attribution from key not `user`; admin endpoints require `admin` scope; CORS restricted by default; refuse to listen on a non-loopback address with auth off.
- **Tests:** each scope, rate limit, revoked key, timing-safe compare, secrets absent from logs.
- **Acceptance:** [architecture/SECURITY_MODEL.md](architecture/SECURITY_MODEL.md) matches the code; leaked-secret scan of the diff.

### Phase 10 — Dashboard
- **Problem:** dashboard depends on the old endpoints; ships demo data; lives under `docs/`.
- **Features:** moved to `app/static/`; views built from what the API actually returns (overview, request log with detail, cache by tier, routing decisions, cost with estimate labels, provider health from real error/latency data); no fabricated data (demo dataset removed, replaced with proper empty/disconnected states); keyboard and screen-reader basics; light theme.
- **Tests:** JS unit tests for the aggregation functions; manual responsive check, with screenshots in `docs/`.

### Phase 11 — Performance and benchmarking
- **Features:** a local **fake OpenAI-compatible upstream** with configurable latency; a load generator; measurements of gateway overhead (miss path), exact-hit latency, semantic-hit latency, throughput and error rate at several concurrency levels, with environment recorded; embedding and FAISS profiling before any optimization; CI `--dry-run` replaced by a small real run against the fake upstream.
- **Acceptance:** `docs/benchmarks/` holds methodology, raw results, and limits. No provider-savings claim without provider credentials.

### Phase 12 — Testing and quality engineering (continuous)
Integration tests against real Redis (CI service container); fake-upstream end-to-end tests; failure-path matrix; test isolation (temp dirs instead of `./data`); coverage report; CI runs lint, tests, a Docker build, and the benchmark smoke.

### Phase 13 — Deployment and operations
Image without dev dependencies; Redis not published to the host and password-protected by default in compose; compose health checks and resource notes; `/health` (liveness) and `/ready` (dependencies); env documentation; backup/restore notes for SQLite and the FAISS snapshot; runbook; troubleshooting. Docker daemon was unavailable at baseline, so the build must be verified, or reported as unverified.

### Phase 14 — Final integration and release
README rewrite; API docs; [LEARNING_GUIDE.md](LEARNING_GUIDE.md); [INTERVIEW_PREPARATION.md](INTERVIEW_PREPARATION.md) (grounded only in shipped code); migration guide; changelog; full verification pass; tag candidate. Not merged to `main` automatically.

## Documentation deliverables by phase

Architecture docs are written with the phase that creates the behavior, so they describe code that exists:
`SYSTEM_DESIGN` and `REQUEST_LIFECYCLE` (Phase 2–3, updated as stages land) · `SECURITY_MODEL` (9) · `DATABASE_DESIGN` (8) · `FAILURE_HANDLING` (7) · `SCALABILITY` (11/13).
