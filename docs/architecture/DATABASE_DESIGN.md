# Database design

NightJar keeps one SQLite file (`SQLITE_DB_PATH`, default `./data/telemetry.db`, WAL mode) for telemetry and API keys, and Redis for cached response payloads. This page describes what is there now and the reasoning for keeping it.

## Is SQLite still the right choice?

Yes, for what this is: a single-node gateway whose writes are one small row per request, with a handful of dashboard reads. The audit found no problem caused by SQLite. The problems were in how it was used (silent write failures, an unversioned schema, per-request connections), and those are fixed or tracked below. The conditions that would justify Postgres are real, and none holds today:

| Condition | Why it would matter | Today |
|---|---|---|
| More than one NightJar replica sharing telemetry and keys | SQLite is a local file | Single instance (documented limit) |
| Sustained writes beyond one writer's capacity | SQLite serialises writers | One row per request; not measured as a bottleneck (Phase 11 measures write cost) |
| Analytical queries over months of history | Needs real indexes, partitioning | Retention is opt-in; history grows unbounded by default |

Migrating would add a server to operate and a migration framework (Alembic) for a schema of three tables. If it becomes necessary, the access code is isolated in `app/telemetry.py` and `app/keys.py`.

## Tables

### `telemetry`: one row per proxied `/v1/chat/*` request that reached the pipeline

| Column | Meaning |
|---|---|
| `request_id` (unique) | The `X-Request-ID` returned to the caller, so a failure report maps to one row |
| `timestamp` | UTC ISO-8601 at completion |
| `model_requested`, `model_used` | What the caller asked for vs. what ran (differs only under routing) |
| `tier`, `routing_reason` | Router classification; prefixed `not_applied(<mode>):` when it was computed but not used |
| `cache_hit`, `cache_tier`, `similarity_score` | Hit, `exact`/`semantic`/`coalesced`, similarity for semantic hits |
| `prompt_tokens`, `completion_tokens` | From provider usage. **NULL = unknown** (e.g. a stream that sent no usage), never 0 |
| `estimated_cost_usd` | **Estimate** from a static price table (`app/costs.py`); 0 for cache hits; NULL when tokens are unknown |
| `latency_ms` | Until the response finished, including a full stream |
| `compressed`, `compression_ratio` | Whether context compression ran |
| `api_key_name` | Authenticated key name (from the key, never from the request body) |
| `http_status`, `error_code` | What the caller received; NightJar error code for failures |

Failed requests are recorded (previously only successes with a model set were). Requests rejected before the pipeline (bad auth, malformed JSON) are not, on purpose: they would inflate totals and skew hit rates. Index: `timestamp`.

### `request_log`: written by LiteLLM's success callback

Only requests that actually reached a provider; cost from `litellm.completion_cost`. It has its own `request_id` (LiteLLM's call id), so it **cannot be joined to `telemetry`**. Merging the two tables was planned and **not done**: the join key would need to be threaded through LiteLLM's callback, and `telemetry` now carries the fields the dashboard needs. Treat `request_log` as the more accurate cost source for non-streaming provider calls and `telemetry` as the complete request record.

### `api_keys`

`name` (unique), `key_hash` (SHA-256, unique), `key_hint`, `scopes`, `allowed_models`, `rate_limit_rpm`, `created_at`, `revoked_at`. Revoked keys stay for audit.

## Migrations

`PRAGMA user_version` holds the schema version. `init_db()` applies forward-only migrations in order at startup and records the version. Migrations only **add columns**, so existing rows are never rewritten or dropped, and each step checks whether the column already exists so a half-finished run can be repeated. A database from NightJar 1.x (version 0) is upgraded in place; this is tested against a copy built from the 1.x schema with a row in it.

| Version | Change |
|---|---|
| 1 | `telemetry.api_key_name`, `http_status`, `error_code`, `cache_tier` |

Upgrade: start the new version against the old file. Downgrade is not supported (older code ignores the extra columns, which in practice works, but is untested). **Back up `telemetry.db` before upgrading production data.** Tables created by older versions that are no longer used (`threshold_history`) are left in place; they can be dropped by hand.

## Failure behaviour and retention

- A telemetry write that fails does **not** fail the request, but is no longer silent: it is logged (`telemetry.write_failed`), counted, and `/health` reports `telemetry_write_failures`.
- Writes run in a worker thread so they cannot block the event loop. Each write opens a short-lived connection under a process-wide lock; there is no batching queue (not built, and not shown to be needed).
- `TELEMETRY_RETENTION_DAYS` deletes rows older than N days at startup. **The default is 0 (keep everything): pruning is opt-in**, so upgrading never removes existing data. It runs only at startup, so a long-running process does not prune until restart.

## Known gaps

- Cost accuracy: `estimated_cost_usd` uses a short static table and a default rate for unknown models; `request_log.total_cost` is better where it exists.
- `/admin/savings` still compares against a fixed gpt-4o price. It is an estimate and labelled as one in the API docs.
- Two cost methods and two tables remain (above).
- Pagination is limit/offset with limit capped at 500; a cursor would be needed for deep paging over large tables.
- Concurrency of many writers was not benchmarked.
