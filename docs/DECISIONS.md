# Architecture Decision Records

> **Naming note.** This project was called ContextForge until November 2026. ADRs 001–006 below are kept as written, so they still say "ContextForge" and mention
> `CONTEXTFORGE_API_KEYS`. That variable still works (deprecated alias of `NIGHTJAR_API_KEYS`). Decisions made during the NightJar modernization start at ADR-007.
> ADR-003 says SQLite is accessed through SQLModel/SQLAlchemy; that was never the case in the code (raw `sqlite3`), and the packages have been removed.

## ADR-001: FAISS over Qdrant for MVP

**Status:** Accepted  
**Date:** 2025-03-25

### Context
We need a vector index to store and search prompt embeddings for semantic cache lookups. Two primary options were evaluated:
- **FAISS** — Facebook's in-process vector similarity search library
- **Qdrant** — A dedicated vector database requiring a separate service

### Decision
Use **FAISS** (Flat index) for the MVP.

### Rationale
- **Zero infrastructure:** FAISS runs in-process — no additional service to deploy, monitor, or scale
- **Simplicity:** A flat index is trivial to implement and debug for MVP-scale data
- **Performance:** For sub-100K vectors, FAISS Flat is fast enough without approximate indexing
- **CPU-only:** No GPU required, aligns with local-first constraint

### Upgrade Path
When the vector count surpasses ~100K or we need persistence/filtering, migrate to Qdrant:
1. Add `qdrant` service to `docker-compose.yml`
2. Swap `app/vector_store.py` implementation (interface remains the same)
3. One-time re-indexing of cached embeddings

---

## ADR-002: Rule-Based Classifier First

**Status:** Accepted  
**Date:** 2025-03-25

### Context
Model routing requires classifying prompt complexity (simple vs. complex) to select the optimal model. Options:
- **Rule-based:** Token count thresholds + keyword matching
- **ML-based:** Fine-tuned classifier on labeled prompt data

### Decision
Use **rule-based heuristics** for the initial implementation.

### Rationale
- **No labeled data required:** ML classifiers need a labeled dataset that doesn't exist yet
- **Ships faster:** Rules can be defined and tuned immediately via `config/routing_rules.yaml`
- **Transparency:** Rules are inspectable and debuggable — no black-box predictions
- **Good enough:** For the MVP, token count + keyword lists capture 80%+ of routing decisions

### Upgrade Path
After accumulating telemetry data with the rule-based router, use it as labeled training data for an ML classifier in a future phase.

---

## ADR-003: SQLite for Telemetry

**Status:** Accepted  
**Date:** 2025-03-25

### Context
We need persistent storage for per-request telemetry data (model used, latency, cost, cache hits). Options:
- **PostgreSQL** — Full-featured relational database
- **SQLite** — Embedded, serverless database

### Decision
Use **SQLite** via SQLModel/SQLAlchemy.

### Rationale
- **Zero infrastructure:** No database server to provision, configure, or maintain
- **Solo developer:** For a single-instance MVP, SQLite's concurrency limitations are irrelevant
- **Portable:** The telemetry database is a single file, easy to back up or inspect
- **SQLModel compatibility:** SQLModel/SQLAlchemy abstracts the engine — migration to Postgres requires only a connection string change

### Upgrade Path
When scaling beyond a single instance or needing concurrent write access:
1. Add `postgres` service to `docker-compose.yml`
2. Change `SQLITE_DB_PATH` to a `DATABASE_URL` connection string
3. Run Alembic migrations

---

## ADR-004: all-MiniLM-L6-v2 as Embedding Model

**Status:** Accepted  
**Date:** 2025-03-25

### Context
Semantic caching requires an embedding model to convert prompts into dense vectors for similarity search. Key requirements:
- Must run locally on CPU (no GPU, no API calls)
- Must be fast enough for real-time inference on every request
- Embedding dimensionality should balance quality vs. index size

### Decision
Use **sentence-transformers/all-MiniLM-L6-v2**.

### Rationale
- **CPU-fast:** ~14ms per embedding on CPU — negligible latency overhead
- **Small footprint:** 80MB model, 384-dimensional output vectors
- **Quality:** Achieves strong performance on semantic textual similarity benchmarks (STS-B)
- **Well-supported:** Part of the sentence-transformers library with extensive documentation
- **Proven:** Widely used in production semantic search and caching systems

### Upgrade Path
If higher embedding quality is needed for domain-specific prompts:
1. Evaluate larger models (e.g., all-mpnet-base-v2 at 768-dim)
2. Fine-tune on domain-specific prompt pairs
3. Update `FAISS_INDEX_PATH` dimension parameter and re-index

---

## ADR-005: Opt-In Bearer-Token Auth Over a Mandatory Auth Layer

**Status:** Accepted
**Date:** 2026-09-06

### Context
ContextForge originally shipped with no authentication at all — reasonable for a single developer running it on localhost, but a real gap the moment it's exposed to a team or a shared environment. Two options were evaluated:
- **Mandatory auth:** require credentials unconditionally, breaking the zero-friction local setup documented in the README
- **Opt-in bearer-token auth:** off by default, enabled by setting `CONTEXTFORGE_API_KEYS`

### Decision
Add **opt-in bearer-token authentication** (`app/auth.py`), applied as a FastAPI dependency to every endpoint except `GET /health`. Disabled when `CONTEXTFORGE_API_KEYS` is unset.

### Rationale
- **Preserves the local-dev experience:** `docker compose up` still works with zero config, matching the existing "Local Setup" instructions
- **Real enforcement when enabled:** constant-time comparison (`hmac.compare_digest`) against a list of tokens, not a single shared secret — supports per-client tokens without a user database
- **`/health` stays open:** load balancers and container orchestrators must be able to probe liveness without credentials
- **Not a replacement for a real identity system:** this is bearer-token gateway auth, not OAuth/JWT/RBAC — sufficient for a small number of trusted clients (its actual use case), not a multi-tenant SaaS

### Upgrade Path
If ContextForge needs to support many distinct callers with different quotas or permissions, replace the flat token list with per-key records (rate limits, allowed models, usage attribution) backed by the existing SQLite database.

---

## ADR-006: E2E Tests Isolated by pytest Marker, Not Skip-on-Missing-Key

**Status:** Accepted
**Date:** 2026-09-06

### Context
`tests/test_e2e.py` runs the real FastAPI app lifespan (real `ProxyClient`, real `Embedder`, real FAISS/Redis) against live providers, and was originally skipped only via `pytestmark = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), ...)`. CI's `ci.yml` sets a dummy `OPENAI_API_KEY` for the *mocked* test suite (it doesn't need a real key, just a non-empty string so `Settings` validation passes), which meant the skip condition was always false in CI — the live-API tests ran for real, failed authentication against OpenAI, and — because they mutate the shared FastAPI `app` singleton's `app.state` via the real lifespan and never unset it — left `app.state.proxy_client` pointing at a real (unmockable) `ProxyClient` for whichever test module ran next in the same process. Result: 24 of 171 tests failed on every CI run for months (`test_e2e.py` itself, plus collateral failures in `test_proxy.py`/`test_router.py`), while `deploy.yml` had independently worked around the same root cause with a `-k "not test_e2e"` filter — the two workflows had silently diverged.

### Decision
1. Mark `test_e2e.py` with `@pytest.mark.e2e` and register it in `pyproject.toml`.
2. Default to excluding it everywhere via `addopts = "-m 'not e2e'"`, so both `ci.yml` and `deploy.yml` (and any local `pytest tests/`) get the same behavior from one source of truth instead of two workflow-specific flags that can drift.
3. Decouple the live-test skip condition from `OPENAI_API_KEY` presence alone — require an explicit `RUN_E2E_TESTS=1` opt-in *and* a real key, so a CI convenience placeholder can never accidentally enable live calls again.
4. Add a `finally` block to the e2e module's fixture that deletes the `app.state` attributes the real lifespan set, so even an intentional `-m e2e` run can't leak real objects into a later test module.

### Rationale
- **One source of truth:** marker + `addopts` in `pyproject.toml` can't drift between workflow files the way two hand-written `-k`/`-m` flags did
- **Fails safe, not silent:** a future CI env var change can no longer accidentally re-enable live calls, because the marker default doesn't depend on any specific env var
- **Defense in depth:** the state-cleanup `finally` block means the isolation doesn't depend solely on remembering the marker filter every time

---

## ADR-007: OpenAI-shaped errors, 400 for validation, 502 for provider failures

**Status:** Accepted (NightJar 2.0) · **Date:** 2026-11-13

### Problem
Validation errors came back as FastAPI's `{"detail": [...]}` with status 422, unexpected exceptions as a bare `Internal Server Error`, and provider failures were passed through with the provider's own status and text. A provider 401 reached callers as a 401, which tells them their own key is wrong. A client library that parses the OpenAI `error` object could not read most of NightJar's errors, and a mid-stream failure produced a truncated stream plus a server traceback.

### Decision
One error shape (`{"error": {message, type, param, code}}`) for every error. Validation → 400 `invalid_request_error`. Provider failures are translated (`app/errors.py:from_upstream`): 401/403 → 502, 5xx → 502, timeout → 504, 429 → 429. Errors after a stream has started are in-band SSE `error` events. Provider text is redacted and truncated.

### Why this and not the alternatives
- *Keep 422 and only change the body:* smaller break, but OpenAI uses 400 for invalid requests and the point is to be a drop-in endpoint. The status code is the part clients branch on (retry logic); 400 is the one they already treat as "don't retry".
- *Pass provider statuses through unchanged:* simplest, and what the old code did, but it makes a gateway configuration fault indistinguishable from a caller fault.
- *Hide all provider detail:* safest, but a "model not found" or "context too long" message is exactly what the caller needs. Truncation plus redaction keeps the useful part.

### Costs
Breaking for any client that parsed `detail` or expected 422/`"429"` codes. Documented in `docs/API.md` under *Errors*. Six existing tests asserted the old behavior and were updated deliberately.

### Interview framing
"A gateway has two kinds of failure, the caller's and its own, and they need different status codes. I mapped provider 401s to 502 because returning 401 would make the caller rotate a key that was fine."

