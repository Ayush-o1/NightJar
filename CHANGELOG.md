# Changelog

## 2.0.0 (unreleased, branch `feature/nightjar-modernization`)

Renamed from ContextForge. Migration guide with every breaking change: [docs/operations/MIGRATION_2.0.md](docs/operations/MIGRATION_2.0.md). Why each change was made: [docs/TECHNICAL_AUDIT.md](docs/TECHNICAL_AUDIT.md) and the ADRs.

### Security and safety
- One 10 MB request could freeze the gateway for over four minutes (tiktoken is quadratic on long whitespace-free text, run on the event loop). Now: 413 before reading, text budget, bounded tokenization.
- Auth: modes `auto`/`required`/`off` (Docker: `required`), hashed scoped API keys with a CLI, per-key model allow-lists and rate limits, admin scope, CORS closed by default.
- Server-side upstream errors use fixed messages; provider text is truncated and secrets redacted; a provider 401 is reported as 502.

### Correctness
- Response cache rebuilt: always-on exact tier keyed on a canonical fingerprint; semantic tier opt-in, partitioned, guarded; eligibility rules; single-flight; measured false-hit rate published. Adaptive threshold removed.
- Model routing is opt-in (`nightjar/auto`); the requested model is honoured by default.
- Context compression no longer separates tool calls from their replies and memoises summaries.
- OpenAI-shaped errors everywhere; multimodal message content accepted; streaming errors are in-band after the first byte.

### Reliability
- Explicit timeout (60 s; LiteLLM's default is 6000 s), error-class retry and cooldown policy, concurrency limit with 503 + `Retry-After`, stream cancellation on client disconnect.

### Observability
- `X-Request-ID` on every response and in telemetry; failed requests recorded with status and error code; per-key attribution; streamed requests get token counts; failed telemetry writes are counted and shown in `/health`; additive schema migrations.

### Engineering
- Locked dependencies (`uv.lock`); FAISS, sqlmodel, sqlalchemy and aiofiles removed; OpenTelemetry optional.
- Tests: 175 → 462, cache tests against a real Redis, reliability tests against a fake provider over real HTTP.
- Benchmarks that measure (`gateway_bench.py`, `cache_eval.py`); the `--dry-run` benchmark that generated numbers from a random number generator was removed.
- Dashboard shows only real data, marks failures, moved to `app/static/`.
- Docker: CPU-only PyTorch, compose overrides `REDIS_URL`, Redis not published, `NIGHTJAR_AUTH=required`.

### Known gaps
See "Known limitations" in the README and [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).

## 1.0.0 and earlier

ContextForge. See git history and tags `v0.1.0`–`v1.0.0`.
