# NightJar API Reference

> v1.0.0 — Full Multi-Provider (LiteLLM) Release

---

## Base URL

```
http://localhost:8000
```

---

## Authentication

Send `Authorization: Bearer <key>`. Keys come from `python -m app.keys create --name <name> --scopes chat[,admin] [--rpm N] [--models a,b]` (stored hashed, shown once) or from the `NIGHTJAR_API_KEYS` environment variable (full access, the simple option).

`NIGHTJAR_AUTH` controls enforcement: `auto` (default; enforced as soon as any key exists, open on a fresh local install), `required` (always; the Docker image sets it), `off` (explicit opt-out). `GET /health` never needs a key.

| Scope | Allows |
|---|---|
| `chat` | `POST /v1/chat/completions` |
| `admin` | `/v1/telemetry*`, `/v1/cache*`, `/admin/*` |

A wrong or missing key is `401` with `WWW-Authenticate: Bearer`; a key without the needed scope is `403 insufficient_scope`; a per-key request limit is `429 rate_limit_exceeded` with `Retry-After`; a model outside the key's or the gateway's allow-list is `403 model_not_allowed`. This is separate from the provider keys (`OPENAI_API_KEY` etc.), which authenticate NightJar to the providers.

---

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | Chat completions (OpenAI-compatible, tools supported, multi-provider via LiteLLM) |
| `GET` | `/health` | Health check |
| `GET` | `/v1/telemetry` | Paginated telemetry records |
| `GET` | `/v1/telemetry/summary` | Aggregated telemetry statistics |
| `GET`/`POST` | `/v1/threshold*` | Removed in 2.0, answers `410` |
| `GET` | `/v1/cache/stats` | Cache statistics |
| `DELETE` | `/v1/cache` | Flush entire cache |
| `DELETE` | `/v1/cache/{key}` | Invalidate a specific cache entry |
| `GET` | `/admin/usage` | Cost/token usage summary with filters |
| `GET` | `/admin/logs` | Paginated raw request log |
| `GET` | `/admin/savings` | Total savings (cache + routing) breakdown |

---

## `POST /v1/chat/completions`

OpenAI-compatible chat completions endpoint. Supports both streaming and non-streaming requests.

### Request Body

NightJar accepts any OpenAI-compatible `model` string. You can also specify **provider-prefixed model names** to route through LiteLLM to alternate providers:

```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {"role": "user", "content": "What is the capital of France?"}
  ],
  "temperature": 0.7,
  "stream": false
}
```

**Multi-provider examples** — just change the `model` field:

```json
{ "model": "groq/llama3-8b-8192",       "messages": [...] }  // Groq
{ "model": "gemini/gemini-1.5-pro",     "messages": [...] }  // Google Gemini
{ "model": "anthropic/claude-3-haiku",  "messages": [...] }  // Anthropic
{ "model": "mistral/mistral-small",     "messages": [...] }  // Mistral
{ "model": "ollama/llama3",             "messages": [...] }  // Local Ollama
```

All caching, routing, compression, and telemetry features work transparently regardless of which provider is used.

### Response (non-streaming)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "gpt-3.5-turbo-0125",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "The capital of France is Paris."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 14, "completion_tokens": 8, "total_tokens": 22}
}
```

### Response Headers

| Header | Values | Description |
|--------|--------|-------------|
| `X-Cache` | `HIT` / `MISS` | Whether the response came from the semantic cache |
| `X-Model-Tier` | `simple` / `complex` | How the router classified the prompt |
| `X-Model-Selected` | e.g., `gpt-3.5-turbo`, `gpt-4o` | The model actually used for the upstream call |
| `X-Compressed` | `True` / `False` | Whether context compression was applied |
| `X-Compression-Ratio` | e.g., `0.65` | Ratio of compressed to original token count |
| `X-Similarity` | e.g., `0.97` | Cosine similarity score (only present on cache hit) |

### Model selection

| `model` in the request | Result |
|---|---|
| a real model name (`gpt-4o`) | Used as-is (default `ROUTING_MODE=explicit`) |
| `nightjar/auto` | NightJar classifies the prompt and picks the simple- or complex-tier model |
| any model, with `ROUTING_MODE=always` | NightJar picks the tier model, ignoring the request (pre-2.0 behaviour) |

`X-Model-Selected` and `X-Model-Tier` show what was used. The telemetry `routing_reason` is prefixed `not_applied(<mode>):` when the router classified the prompt but did not switch models, so the tier it would have used is always visible. **Changed in 2.0:** before, every request was routed.

### Special Request Headers

| Header | Description |
|--------|-------------|
| `X-NightJar-Cache` | `bypass`: skip lookup and store for this request. `refresh`: skip the lookup, call the provider, overwrite the stored entry. |
| `X-NightJar-Model-Override` | Force a specific model regardless of routing (e.g. `gpt-4o`). Subject to `ALLOWED_MODELS` (`403 model_not_allowed`). |
| `X-NightJar-No-Compress` | Set to `true` to skip context compression for this request |

### Request limits

| Limit | Default | Setting | Response |
|---|---|---|---|
| Request body size | 8 MiB | `MAX_REQUEST_BYTES` | `413`, code `request_too_large` |
| Characters of text across all messages | 2,000,000 | `MAX_PROMPT_CHARS` | `400`, code `context_length_exceeded` |
| `messages` | at least 1 | | `400` |
| `temperature` / `top_p` / `n` | 0–2 / 0–1 / 1–16 | | `400` |

Message `content` may be a string, a list of parts (`{"type":"text",...}`, `{"type":"image_url",...}`), or null (assistant messages that only carry `tool_calls`). Image and audio parts are forwarded but are not tokenized, embedded, or counted against the text budget.

Every response carries `X-Request-ID`. Send your own `X-Request-ID` (letters, digits, `.`, `_`, `-`, up to 64 characters) to have it used instead; anything else is replaced.

### Notes

- When `stream=true`, the response is Server-Sent Events. Streaming requests bypass caching and compression.
- Failures **before** the first chunk (bad model, rate limit, provider down) return an ordinary HTTP error. A failure **after** the stream has started can't change the status code, so it arrives as a final `data: {"error": {...}}` event and **no** `data: [DONE]`. OpenAI SDKs raise on that event.
- On a cache hit no upstream call is made. The response carries `X-Cache: HIT` and `X-Similarity`; a miss carries `X-Cache: MISS`.

---

## Errors

Every error, from every endpoint, has the OpenAI shape:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": "messages", "code": "invalid_request"}}
```

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_request_error` | Validation failed (`param` names the field), invalid JSON (`code: invalid_json`), prompt over budget, or the provider rejected the request (`code: upstream_invalid_request`) |
| 401 | `authentication_error` | Missing/invalid NightJar API key. `WWW-Authenticate: Bearer` is set. |
| 404 | `invalid_request_error` | Unknown route, or unknown model upstream (`code: model_not_found`) |
| 413 | `invalid_request_error` | Body over `MAX_REQUEST_BYTES` |
| 429 | `rate_limit_error` | The provider rate-limited the request |
| 500 | `server_error` | Bug in NightJar. The message contains the request ID to report. |
| 502 | `upstream_error` | Provider failed (`upstream_error`), or **provider rejected NightJar's own credentials** (`upstream_auth_error`) |
| 503 / 504 | `upstream_error` | Provider unavailable / timed out |

A provider's 401/403 is reported as **502**, never 401: it means the gateway's provider key is wrong, not that the caller's NightJar key was rejected. Provider error text is truncated to 500 characters and credential-looking strings are replaced with `[REDACTED]`.

**Changed in 2.0:** validation errors used FastAPI's `{"detail": [...]}` body with status 422; they are now 400 with the shape above. Upstream 5xx used to be passed through with the provider's status; they are now 502. The `code` of upstream errors used to be the status code as a string (`"429"`); it is now a descriptive code.

---

## `GET /health`

Health check endpoint. Never requires authentication, even when `NIGHTJAR_API_KEYS` is set — it's the readiness/liveness probe.

`status` reflects whether the process is up and serving requests; it stays `"ok"` even if Redis is down, since the semantic cache degrades to a pass-through cache miss rather than failing requests (see [ARCHITECTURE.md](ARCHITECTURE.md)). `redis` reports that dependency's reachability separately.

### Response

```json
{
  "status": "ok",
  "version": "1.0.0",
  "redis": "ok"
}
```

`redis` is one of `"ok"`, `"unreachable"`, or `"unknown"` (not yet initialized).

---

## `GET /v1/telemetry`

Returns paginated telemetry records, newest first. All data is stored locally in SQLite.

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | Maximum records to return (1–500) |
| `offset` | int | 0 | Number of records to skip |

### Response

```json
{
  "records": [
    {
      "request_id": "abc-123",
      "timestamp": "2026-03-27T02:00:00",
      "model_requested": "gpt-3.5-turbo",
      "model_used": "gpt-3.5-turbo",
      "tier": "simple",
      "routing_reason": "token_count:12<=200",
      "cache_hit": false,
      "similarity_score": 0.0,
      "prompt_tokens": 14,
      "completion_tokens": 8,
      "estimated_cost_usd": 0.000029,
      "latency_ms": 450.0,
      "compressed": false,
      "compression_ratio": 1.0
    }
  ],
  "limit": 50,
  "offset": 0
}
```

---

## `GET /v1/telemetry/summary`

Returns aggregated telemetry statistics across all recorded requests.

### Response

```json
{
  "total_requests": 150,
  "cache_hits": 42,
  "avg_latency_ms": 320.5,
  "total_cost_usd": 0.0245,
  "avg_tokens": 35.2,
  "cache_hit_rate": 0.28,
  "p95_latency_ms": 890.0
}
```

### Field Descriptions

| Field | Description |
|-------|-------------|
| `total_requests` | Total number of recorded requests |
| `cache_hits` | Number of requests served from cache |
| `avg_latency_ms` | Average response latency in milliseconds |
| `total_cost_usd` | Estimated total cost (approximate — see note below) |
| `avg_tokens` | Average tokens per request |
| `cache_hit_rate` | Proportion of requests served from cache (0.0–1.0) |
| `p95_latency_ms` | 95th percentile latency |

> **Note:** Cost estimates use hardcoded per-token rates in `app/costs.py`. Actual billing from your LLM provider may differ.

---

## Removed: `/v1/threshold`, `/v1/threshold/history`, `/v1/threshold/evaluate`

These adaptive-threshold endpoints were removed in 2.0 and answer `410 Gone` (`code: endpoint_removed`) for one release. See ADR-008. Set `SIMILARITY_THRESHOLD` and calibrate with `benchmarks/cache_eval.py`.

---

## `GET /v1/cache/stats`

Returns cache statistics: vector count, Redis key count, whether the semantic tier is on, the configured threshold, and in-process counters since start (hits by tier, misses, coalesced requests, guard rejections, store/skip counts, Redis and embedder errors).

### Response

```json
{
  "total_vectors": 0,
  "redis_keys": 148,
  "semantic_enabled": false,
  "similarity_threshold": 0.92,
  "counters": {"exact_hits": 61, "semantic_hits": 0, "coalesced": 3, "misses": 87, "guard_rejections": 0,
               "stored": 85, "not_stored": 2, "redis_errors": 0, "embed_errors": 0}
}
```

> **Note:** If Redis is unavailable, `redis_keys` will return `0` and an error will be logged. The endpoint will not crash.

---

## `DELETE /v1/cache`

Flush the response cache: all vectors in the index and all NightJar keys in Redis (other keys in the same Redis are left alone). This operation is idempotent — calling it on an empty cache returns successfully.

### Response

```json
{
  "status": "ok",
  "vectors_cleared": 150,
  "redis_keys_cleared": 148
}
```

---

## `DELETE /v1/cache/{key}`

Invalidate one cache entry by its key (the hash in the Redis key `nj:v2:r:<key>`). Removes both the vector and the stored response.

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string | The cache key to invalidate |

### Response

```json
{
  "status": "ok",
  "key": "abc123",
  "removed": true
}
```

---

## Dashboard

The dashboard at `/dashboard/` consumes the endpoints above. If the backend is unreachable it falls back to a demo dataset and labels itself as such. See [ARCHITECTURE.md](ARCHITECTURE.md#dashboard).
