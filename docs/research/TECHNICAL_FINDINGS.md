# Technical findings

Evidence gathered before designing, and the conclusion each piece of evidence leads to. Citation numbers refer to [SOURCE_REFERENCES.md](SOURCE_REFERENCES.md). "Measured" means I ran it on this machine; see [BASELINE.md](../BASELINE.md).

## F1. Semantic similarity does not imply answer equivalence

**Evidence.** Measured with `all-MiniLM-L6-v2`: "Convert 10 miles to kilometers" vs. "Convert 10 kilometers to miles" scores 0.992; "ascending" vs. "descending" 0.951; "2022" vs. "2023" 0.944. A genuine paraphrase ("reset" vs. "change my password") scores 0.860. Published work reaches the same conclusion from a different direction: a low default threshold gives many false hits [5], and a single static threshold is the structural problem [6]. Redis's own product defaults to 0.85 and tells users to tune on their own data [3].

**Why thresholds can't fix it.** The wrong pairs sit above the right pair. Any threshold that accepts the paraphrase also accepts the unit-swap. Raising it to reject the swap also rejects every paraphrase.

**Conclusion.**
1. Make an **exact-match tier** (canonical hash of the entire request) the default cache. It is always correct for deterministic requests and covers the common production case of identical repeats (retries, polling, shared system prompts with templated inputs).
2. Make the **semantic tier opt-in** and surround it with gates that remove the failure classes we can detect cheaply: partition by model + generation parameters + system prompt + tool schema; require equal numbers, negations and quoted strings between the two prompts; use a conservative threshold.
3. **Measure its false-hit rate** on a labeled pair set and publish the number with its limits. Gates catch 2022-vs-2023 but not miles-vs-kilometers, because both prompts contain the same words in a different order. Documentation has to say that.
4. Remove the adaptive threshold (it rewards hit rate, not correctness) and keep a manual, offline calibration tool instead.

## F2. A cache key must include everything that changes the response

**Evidence.** Audit C-1: today only message text is embedded and the model name never reaches the search.

**Conclusion.** Canonicalize the request into a *fingerprint* of: resolved model, temperature, top_p, max_tokens, stop, seed, `n`, tools/functions + tool_choice, response_format, and all non-final messages as structured data. Exact tier = hash of the full fingerprint plus final message. Semantic tier = ANN search restricted to the same *partition key* (the fingerprint without the final user message). Responses that should not be cached are excluded at write time: tool calls, `n>1`, finish reasons other than `stop`, and requests with `temperature` above a configurable cutoff (default 0.3; a sampling-heavy request's answer is not meant to repeat). `Cache-Control: no-store` style opt-outs are honored.

## F3. FAISS flat index fits, but needs lifecycle management

**Evidence.** Audit C-5: vectors outlive their Redis entries and persistence happens only at shutdown. `IndexFlatIP` brute-force search is exact and fast for tens of thousands of vectors; the project's own ADR-001 estimates 100K as the point to revisit, and I'm not going to claim a number without benchmarking it (Phase 11 does).

**Conclusion.** Keep FAISS in-process. Store entry metadata (partition, expiry, response key) next to the vector. Evict expired entries on a schedule and in `lookup` when the Redis entry is gone. Rebuild lazily rather than on every delete. Persist on a timer and at shutdown, with atomic file replace. Reads and writes go through one lock or a copy-on-write swap. Qdrant/pgvector remain the documented upgrade path for multi-replica deployments, where the real constraint is shared state, not search speed.

## F4. Embedding and tokenization must not run on the event loop

**Evidence.** Measured: tiktoken on 200 KB of whitespace-free text took 8.6 s and is quadratic; a 10 MB request froze the process for more than four minutes. Sentence-transformers inference and FAISS are also CPU-bound synchronous calls inside `async def` handlers.

**Conclusion.** (1) Enforce a maximum body size *before* parsing and a maximum message/character budget *before* tokenizing, so the pathological input never reaches tiktoken. (2) Run embedding and tokenization through `asyncio.to_thread` / a small bounded executor. (3) Estimate tokens for routing with a cheap, linear character-based bound when the text is over a length cutoff; reserve exact tiktoken counts for bounded inputs. Verified by test: a 1 MB hostile request returns `413` quickly and does not stall `/health`.

## F5. Reliability: configure LiteLLM's Router, add what it lacks

**Evidence.** The installed Router (1.95.1) accepts `num_retries`, `timeout`, `allowed_fails`, `allowed_fails_policy`, `cooldown_time`, `retry_policy`, `max_fallbacks`, `disable_cooldowns` (checked by introspection) and, per its docs [1], retries within a model group, falls back across groups, and cools down failing deployments. The baseline code sets only `num_retries=2` and `retry_after=0`: **no timeout** (so waits are bounded only by whatever default the library applies, which I have not verified), no explicit cooldown policy. A single rejected request put the only deployment of a group into a five-second cooldown.

**Conclusion.**
- Set explicit timeouts per request and an overall deadline.
- Use `allowed_fails_policy` / `retry_policy` so that authentication and bad-request errors are **never retried** and never trip a cooldown (retrying a 401 is wasted latency; cooling down a deployment because the caller's request was invalid is wrong). Rate-limit, timeout and 5xx errors do.
- Add a gateway-level **retry budget** (retries allowed only while they stay below a fraction of recent traffic [8]) and a global **concurrency limit** with a fast `503` + `Retry-After` when saturated, instead of unbounded queuing.
- Read fallback chains from config rather than a hard-coded list.
- Honor client disconnects: if the caller is gone, cancel the upstream call.

Not building: a bespoke circuit-breaker state machine. The Router's cooldown is the same idea, and I'd test its configuration instead of replacing it.

## F6. Streaming needs different treatment for telemetry and errors

**Evidence.** Audit E-1/E-2: errors after the first byte are raised into the generator and truncate the stream; `BaseHTTPMiddleware` records latency at header time and zero tokens. OpenAI documents `stream_options.include_usage` for a final usage chunk and notes that chunk can be absent on interruption [10].

**Conclusion.** Replace `BaseHTTPMiddleware` with a small pure-ASGI middleware for request IDs, body limits and timing (the Starlette docs recommend this for streaming but I couldn't fetch them, see SOURCE_REFERENCES #12; I'm doing it because it makes response-body accounting explicit and testable). Record telemetry from the proxy layer when the stream **completes or aborts**, with `status` in `{ok, error, client_disconnect}`. Request `include_usage` upstream; when usage is missing, store NULL tokens and mark cost as unknown. A stream error after headers is emitted as an in-band SSE `error` event followed by termination, because the HTTP status can no longer change.

## F7. Routing: keep rules, make them opt-in, measure properly

**Evidence.** Measured routing accuracy is 92.8%, but on 1,000 prompts of ≤13 words aligned with the router's own keywords (circular). The router currently overrides the caller's chosen model on every request. RouteLLM [7] shows routing is judged on cost versus quality, which this project has no data for.

**Conclusion.** A requested concrete model is honored. Routing happens only when the caller sends the reserved model alias (`nightjar/auto`) or when the operator enables `ROUTING_MODE=always` explicitly. Decisions use whole-word/phrase matching and return a structured explanation (rule, matched text, token estimate). Model names come from config. The labeled set gets a "hard cases" section (long simple prompts, short hard prompts) so accuracy isn't flattered by the data. No ML classifier: no quality labels exist to train or evaluate one.

## F8. Telemetry: one record per request, written off the hot path

**Evidence.** Audit D-1/D-2: two tables with unrelated IDs and two cost calculations; silent `except: pass`; per-write connections; synchronous SQLite calls inside async handlers.

**Conclusion.** One `requests` table keyed by the request ID echoed in the response header, holding routing, cache, usage, cost, latency and outcome. Cost computed once from LiteLLM's price data, with `cost_source` recorded (`provider_usage`, `estimated`, `unknown`). Writes go through a bounded in-memory queue drained by one background task in batches; when the queue is full, records are dropped **and counted**, and the count is exposed in `/health`. Old tables are migrated, never dropped. SQLite stays: one writer, WAL mode, a single node. Details and the migration plan are in [../architecture/DATABASE_DESIGN.md](../architecture/DATABASE_DESIGN.md).

## F9. Security baseline for an LLM gateway

**Evidence.** OWASP LLM02 (sensitive information disclosure) and LLM10 (unbounded consumption) [15]; audit S-1…S-4; the 10 MB request.

**Conclusion.** Auth **on by default** (the service refuses to bind a non-loopback address without keys, unless `NIGHTJAR_AUTH=off` is set explicitly). Keys are stored hashed with an identity, optional model allow-list and rate/spend limits, so cost is attributed to a key rather than to a client-supplied `user` string. Prompts and responses are never logged; they are stored only as cache payloads, with TTL. Upstream errors are mapped to a fixed set of messages instead of forwarding raw provider text. Size and rate limits apply before any expensive work.

## What I'm not claiming

- No latency or throughput improvement is claimed anywhere until Phase 11 produces numbers against a controlled fake upstream. Even then it will show gateway overhead and cache-hit latency, not provider savings, because I have no provider credentials in this environment.
- The similarity probe is ten hand-written pairs. Phase 5 builds a larger labeled set and reports precision with its limitations.
