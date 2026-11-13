# Troubleshooting

## Every request returns 401

Auth is enforced. Either a key exists (env or key store) or `NIGHTJAR_AUTH=required` is set (the Docker image does). Send `Authorization: Bearer <key>`. With none configured: set `NIGHTJAR_API_KEYS=<key>` in `.env`, or `python -m app.keys create --name app --scopes chat`, or (local development only) `NIGHTJAR_AUTH=off`. The startup log line `auth.required_but_no_keys` says this when it applies.

## 403 `insufficient_scope`

The key lacks the needed scope: chat keys cannot reach `/admin/*`, `/v1/telemetry*` or `/v1/cache*`; admin-only keys cannot call the model. Create a key with both (`--scopes chat,admin`), or use an env key.

## 403 `model_not_allowed`

The model is outside `ALLOWED_MODELS` or the key's model list. The message says which.

## 429 from NightJar vs. from the provider

Check `error.code`: `rate_limit_exceeded` is NightJar's per-key limit (`Retry-After` tells you when); `upstream_rate_limit` is the provider's.

## 502 `upstream_auth_error`

The **provider** rejected NightJar's provider key (`OPENAI_API_KEY` etc.). It is a gateway configuration problem, not your NightJar key. Check the provider key in `.env`; the provider's message is in the server log (`upstream.error`), never in the response.

## 502 or 503 for a while after provider errors

The circuit breaker (LiteLLM cooldown) opened after repeated timeouts/429s/5xx and skips that deployment for `UPSTREAM_COOLDOWN_SECONDS` (default 10 s). Requests fail fast during that window and recover by themselves. See [FAILURE_HANDLING](../architecture/FAILURE_HANDLING.md).

## 503 `gateway_overloaded`

`MAX_CONCURRENT_UPSTREAM` provider calls are already in flight. `/health` shows `upstream.in_flight`, `limit`, `rejected`. Retry after `Retry-After`, raise the limit, or find out why the provider is slow.

## 413 `request_too_large` / 400 `context_length_exceeded`

Body over `MAX_REQUEST_BYTES` (8 MiB) or text over `MAX_PROMPT_CHARS`. Images are not counted as text but do count toward the body size.

## Cache never hits

- Check `X-Cache` and `X-Cache-Reason` on the response. `BYPASS` with `ineligible:tools`, `ineligible:temperature_above_cutoff` or `ineligible:n_greater_than_1` explains it; `client_bypass` means `X-NightJar-Cache: bypass` was sent.
- Any change in model, temperature, `max_tokens`, system prompt or earlier messages is a different request. Two requests only share an entry when everything but wording is identical.
- `/v1/cache/stats` shows counters (`exact_hits`, `redis_errors`, ...). `redis_errors > 0` means Redis is unreachable (calls time out after 1 s; requests still succeed).
- **In Docker Compose the app must use `redis://redis:6379`** (the compose file sets this, overriding `REDIS_URL` from `.env`). A `localhost` URL inside the container points at the container itself.
- Near-duplicate prompts only hit with `SEMANTIC_CACHE=true`, and only inside the same model/parameters/conversation.

## Semantic tier returns a wrong answer

Known limitation: embeddings score some opposite-meaning prompts above real paraphrases ([CACHE_CORRECTNESS](../benchmarks/CACHE_CORRECTNESS.md)). Raise `SIMILARITY_THRESHOLD`, turn the tier off, or send `X-NightJar-Cache: bypass` for questions where a wrong answer is costly. Flush with `DELETE /v1/cache`.

## Streaming ends with an `error` event and no `[DONE]`

The provider failed after the stream started; the HTTP status could no longer change, so the error is in-band. Handle `data: {"error": ...}` in custom SSE parsers (OpenAI SDKs raise).

## `/health` shows `redis: unreachable`

The process is healthy (status stays `ok`) but the cache is degraded to misses. Check `REDIS_URL` and that Redis is running.

## `/health` shows `telemetry_write_failures > 0`

Telemetry writes are failing (disk full, locked or read-only database). Requests are unaffected; look for `telemetry.write_failed` in the log.

## Startup is slow

With `SEMANTIC_CACHE=true` the embedding model loads at start. With no internet, set `HF_HUB_OFFLINE=1` (the Docker image has the model pre-downloaded unless built with `PRELOAD_EMBEDDING_MODEL=false`). With the tier off nothing is loaded.

## `ModuleNotFoundError: No module named 'app'` when running tests

Run `PYTHONPATH=. uv run pytest tests/`.

## Tests skip the cache tests

No Redis was found. Install `redis-server`, or set `REDIS_TEST_URL=redis://localhost:6379`.

## Dashboard shows "Offline" or empty pages

The dashboard only shows data from the API. With auth on, store an admin key: `localStorage.setItem('nightjar_api_key', '<key>')` in the browser console, then reload.
