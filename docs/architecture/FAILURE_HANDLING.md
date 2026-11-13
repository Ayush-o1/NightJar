# Failure handling

What happens when each dependency or request misbehaves, what is configured, and what is **not** covered. Every row marked *tested* is exercised by a test in the repo; rows marked *untested* are stated as intent only.

## Provider failures

NightJar calls providers through the LiteLLM Router, configured explicitly in `app/proxy.py` (the defaults were not safe: the installed Router's default timeout is 6000 s).

| Error from provider | Retried? | Counts toward cooldown? | Caller sees |
|---|---|---|---|
| 401 / 403 (NightJar's key rejected) | No | **No** | `502 upstream_auth_error` |
| 400 / malformed request | No | **No** | `400 upstream_invalid_request` (message includes the redacted, truncated provider text) |
| 429 | Yes, up to `UPSTREAM_RETRIES` | Yes | `429 rate_limit_error` |
| 5xx | Yes | Yes | `502 upstream_error` (fixed message, no provider text) |
| Timeout (`UPSTREAM_TIMEOUT_SECONDS`) | Yes | Yes | `504 upstream_timeout` |
| Deployment in cooldown | n/a | n/a | fails fast, no provider call (*tested*) |

*Tested* against a fake provider over real HTTP (`tests/test_reliability.py`):

- A 401 or 400 is attempted **once**. Before this change one rejected request put the only deployment into a 5-second cooldown and the next valid request failed with "No deployments available" (reproduced in the baseline, now a regression test).
- A 500 or 429 is attempted `1 + UPSTREAM_RETRIES` times, then fails. Measured against a fake that answers instantly, a request that exhausts 2 retries takes about **4.4 s** in total, because LiteLLM backs off between attempts. Plan client timeouts accordingly.
- Four straight 500s open the breaker: the next request returns in well under 0.5 s without reaching the provider; after the cooldown the provider is called again.
- A hung provider is cut off at the timeout.

### Circuit breaker: what it is and isn't

The "breaker" is LiteLLM's cooldown, configured through `allowed_fails_policy`: after 3 timeouts/429s/5xx within a minute a deployment is skipped for `UPSTREAM_COOLDOWN_SECONDS`. With a single deployment in a model group and no fallback, an open breaker means *every* request fails fast for that period instead of each waiting through retries. That protects the provider and the gateway and is the intended trade, but it does mean a short provider blip produces a longer window of fast 502s.

**Not implemented:** a gateway-level **retry budget** (cap retries to a fraction of traffic). Retries are bounded per request (`UPSTREAM_RETRIES`) and overall load is bounded by the concurrency limit, but under a provider brownout every request can still retry. This was in the roadmap and was not built.

### Fallbacks

Fallback chains are still defined in code (`ProxyClient._build_fallback_map`) with model names chosen at an earlier date, and the `fallbacks:` and `provider_preference:` sections of `config/routing_rules.yaml` are still not read. Some default model names may be out of date; I have not checked them against current provider documentation. Request a provider-prefixed model (`anthropic/<model>`) to bypass the defaults. Moving deployments and chains into validated configuration is **not done**.

## Overload

`MAX_CONCURRENT_UPSTREAM` (default 128) caps provider calls in flight. Past it the gateway answers `503 gateway_overloaded` with `Retry-After: 1` *immediately*; it does not queue. Only requests that actually call a provider hold a slot: cache hits and single-flight followers do not. Slots are released on success, on error, and when a stream ends or the client disconnects (*tested*). `/health` reports `upstream.in_flight`, `limit` and `rejected`. The limit is per process.

## Client disconnects

If a streaming client hangs up, the upstream stream is closed and its slot freed (*tested over a real socket*: the fake provider recorded the aborted stream). For non-streaming requests there is no cancellation: the provider call runs to completion and, if cacheable, is stored. *Untested:* whether LiteLLM aborts the underlying HTTP request on stream close for every provider; verified only against the fake OpenAI-compatible server.

## Streaming errors

Before the first chunk: an ordinary HTTP error. After it: an in-band `data: {"error": ...}` event and no `[DONE]` (*tested*).

## Cache and dependencies

| Failure | Behaviour |
|---|---|
| Redis unreachable | Every cache operation degrades to a miss or no-op; requests succeed (*tested*, also at HTTP level). Redis calls time out after 1 s. |
| Redis payload expired/evicted while a vector remains | Lookup deletes the dangling vector and misses (*tested*) |
| Embedder raises | Semantic tier skipped, exact tier unaffected (*tested*) |
| Corrupt cache payload or vector file | Treated as a miss / ignored at startup (*tested*) |
| Crash | Vectors written since the last snapshot (default every 60 s) are lost; the exact tier is unaffected because payloads live in Redis |
| Leader of identical concurrent requests fails | Followers get the same error, not N retries (*tested*) |
| Summariser fails or returns nothing | Request proceeds uncompressed (*tested*) |

## Telemetry and database

SQLite errors during the telemetry write are still **swallowed silently** (`except Exception: pass`), so a full disk loses data without any signal. This is audit item D-1, scheduled for the telemetry phase. The write runs in a thread so it cannot block the event loop.

## Input

Oversized bodies: `413` before the body is read (declared length) or mid-upload (chunked). Over-long text: `400 context_length_exceeded`. Pathological text cannot stall the tokenizer (bounded counting). A hostile 10 MB request that froze the baseline for over four minutes returns in under a millisecond.

## Known gaps

- No retry budget (above).
- Single-flight and the concurrency limit are per process.
- Fallback chains and model names are code, not configuration.
- Telemetry write failures are invisible.
- Behaviour with real providers was not tested: no credentials were available, so provider-specific error shapes (for example Anthropic's) are known only through LiteLLM's exception mapping.
