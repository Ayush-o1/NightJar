# Migrating from ContextForge 1.x to NightJar 2.0

Everything that changes behaviour, and what to do about it. The numbers behind the reasons are in [../BASELINE.md](../BASELINE.md) and [../TECHNICAL_AUDIT.md](../TECHNICAL_AUDIT.md); the decisions are ADR-007 to ADR-009 in [../DECISIONS.md](../DECISIONS.md).

## Before you upgrade

1. **Back up `data/telemetry.db`.** Startup migrates it in place (additive columns only, nothing is deleted or rewritten; tested against a database written by the real 1.x code), but a backup costs nothing.
2. **Decide how callers authenticate.** See "Access" below. In Docker the new default refuses requests until a key exists.
3. Read the **Behaviour changes** table; at least the first three can surprise a caller.

## Renames (old names still work, deprecated)

| Was | Now | Compatibility |
|---|---|---|
| `CONTEXTFORGE_API_KEYS` | `NIGHTJAR_API_KEYS` | Old name still read; a warning is logged at startup. Removed in the next minor release. |
| `X-ContextForge-Model-Override`, `X-ContextForge-No-Compress` | `X-NightJar-*` | Old headers still accepted (a warning is logged). The new name wins if both are sent. |
| Dashboard `localStorage` key `contextforge_api_key` | `nightjar_api_key` | Old key still read |
| `CONTEXTFORGE_URL` (benchmark script) | `NIGHTJAR_URL` | Old name still read |
| Package, image and log names | `nightjar` | Log events `contextforge.*` are now `nightjar.*`; update any log alerts |
| GitHub URL | `github.com/Ayush-o1/NightJar` | The old URL redirects |

Response headers (`X-Cache`, `X-Model-Selected`, ...) are unchanged.

## Behaviour changes

| # | Change | Why | What to do |
|---|---|---|---|
| 1 | **Your model is used as requested.** Before, every request was silently switched to a tier model (asking for `gpt-4o` with "hi" ran `gpt-3.5-turbo`). Now routing happens only for the model alias `nightjar/auto`, the override header, or `ROUTING_MODE=always`. | Surprising model swaps are not OpenAI-compatible | To keep the old behaviour set `ROUTING_MODE=always`. Look at `routing_reason` (`not_applied(explicit):...`) in telemetry to see what routing would have done before you turn it on. |
| 2 | **Error responses** use the OpenAI shape `{"error": {message,type,param,code}}` everywhere. Validation errors are **400** (were 422 with `{"detail": [...]}`). Provider 5xx and a provider 401/403 are **502** (were passed through; a provider 401 reached your callers as a 401). Upstream `code` is descriptive (was the status as a string). | SDKs parse the `error` object; a gateway fault must not look like the caller's | Update code that parsed `detail`, or expected 422 or `"429"` codes. Table in [../API.md](../API.md#errors). |
| 3 | **Semantic caching is off by default.** An always-on **exact** tier replaces it. | The old semantic cache served wrong answers; 22% of should-not-match pairs at the default threshold ([CACHE_CORRECTNESS](../benchmarks/CACHE_CORRECTNESS.md)) | Set `SEMANTIC_CACHE=true` if you accept that risk. Hit rates for near-duplicate prompts will drop until you do. |
| 4 | **Cache key includes model, parameters and the whole conversation**, not just message text. Requests with tools, `n>1` or temperature above `CACHE_MAX_TEMPERATURE` (default 1.0; unset counts as 1.0) are never cached. | A response for one model/temperature must not serve another | None. Expect more misses than before. |
| 5 | **Redis keys** are now `nj:v2:r:*`. Old `cache:*` keys are ignored. | New key design | Let them expire (TTL), or `redis-cli --scan --pattern 'cache:*' \| xargs redis-cli del`. |
| 6 | **Adaptive threshold removed.** `/v1/threshold`, `/v1/threshold/history`, `/v1/threshold/evaluate` answer **410**. | It rewarded a higher hit rate, not correctness | Set `SIMILARITY_THRESHOLD`; calibrate with `benchmarks/cache_eval.py`. |
| 7 | **Streaming:** a failure before the first chunk is a normal HTTP error; a failure after it is an in-band `data: {"error": ...}` event with no `[DONE]`. | The old code truncated the stream and logged a traceback | OpenAI SDKs raise on the in-band event. Custom SSE parsers should handle an `error` object. |
| 8 | **Limits:** request body over 8 MiB is 413; text over 2,000,000 characters is 400. `/v1/telemetry` `limit` is capped at 500. | A single large request froze the 1.x gateway for over four minutes | Raise `MAX_REQUEST_BYTES` / `MAX_PROMPT_CHARS` if you legitimately need more. |
| 9 | **Provider waits are bounded:** `UPSTREAM_TIMEOUT_SECONDS=60` (LiteLLM's own default is 6000), retries only for 429/5xx/timeouts, 401/400 never retried. A request that exhausts its retries takes about 4.4 s to fail. | Unbounded waits | Tune the three `UPSTREAM_*` variables. |
| 10 | **Overload is shed:** more than `MAX_CONCURRENT_UPSTREAM` (128) provider calls in flight → `503` + `Retry-After: 1`. | No queueing without bound | Raise it if your provider and hardware allow. |
| 11 | **Compression runs after the cache lookup**, only on misses, and never separates a tool call from its replies. | Cache keys were built from non-deterministic summaries | None. |

## Access

| | 1.x | 2.0 |
|---|---|---|
| Auth default | Off | `NIGHTJAR_AUTH=auto`: off until a key exists, then enforced. **The Docker image sets `required`.** |
| Keys | One shared list in `CONTEXTFORGE_API_KEYS` | Env keys (full access) **or** hashed per-key records with scopes, rate limit and model allow-list: `python -m app.keys create ...` |
| Admin endpoints (`/admin/*`, `/v1/telemetry*`, `/v1/cache*`) | Same token as chat | Need the `admin` scope |
| CORS | `*` | No origins; set `CORS_ALLOW_ORIGINS` for browser apps |

**If you run the Docker image and previously had no keys,** requests now return 401 until you set `NIGHTJAR_API_KEYS` (or create keys, or deliberately set `NIGHTJAR_AUTH=off`).

## Settings removed

Unknown environment variables are ignored, so old `.env` files still load. These no longer do anything:

`ENABLE_CACHE`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` (put the password in `REDIS_URL`), `FAISS_INDEX_PATH` (now `VECTOR_INDEX_PATH`, default `./data/vectors.npz`; old `faiss.index*` files can be deleted), `ADAPTIVE_THRESHOLD_*`, `OPENAI_BASE_URL` (was never used).

New settings: `NIGHTJAR_AUTH`, `DEFAULT_RATE_LIMIT_RPM`, `ROUTING_MODE`, `ALLOWED_MODELS`, `SEMANTIC_CACHE`, `CACHE_MAX_TEMPERATURE`, `CACHE_MAX_ENTRIES`, `VECTOR_INDEX_PATH`, `UPSTREAM_TIMEOUT_SECONDS`, `UPSTREAM_RETRIES`, `UPSTREAM_COOLDOWN_SECONDS`, `MAX_CONCURRENT_UPSTREAM`, `MAX_REQUEST_BYTES`, `MAX_PROMPT_CHARS`, `TELEMETRY_RETENTION_DAYS`.

## Data

- **`telemetry.db`** is migrated in place to schema version 1 (adds `api_key_name`, `http_status`, `error_code`, `cache_tier`). Old rows keep their values and get NULL in the new columns. Rows with `prompt_tokens = 0` from before are real zeros or, for streams, unknown; they cannot be told apart.
- `threshold_history` (adaptive threshold) is left in the file, unused. Drop it by hand if you like.
- The tables `request_log` and `telemetry` are unchanged in meaning.
- Nothing is deleted. Retention pruning (`TELEMETRY_RETENTION_DAYS`) is opt-in.
- **Downgrade** is not supported; if you must, restore the backup. (Older code ignores the extra columns in practice; untested.)

## Dependencies and build

`pyproject.toml` + `uv.lock` are the source of truth (`uv sync --frozen`); `requirements.txt` is generated from the lock. `faiss-cpu`, `sqlmodel`, `sqlalchemy` and `aiofiles` are gone; OpenTelemetry is an optional extra (`uv sync --extra otel`). The dashboard moved from `docs/dashboard/` to `app/static/dashboard/`.

## Rollback

Restore the `telemetry.db` backup and run the 1.x image. Redis entries written by 2.0 live under `nj:v2:r:*` and do not interfere with 1.x.
