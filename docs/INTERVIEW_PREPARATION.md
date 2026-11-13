# Interview preparation

Questions an interviewer could realistically ask about this project, with answers grounded in the code and the measurements in this repository. Nothing here describes a feature that isn't implemented; the gaps are listed because "what's missing?" is a standard question and the honest answer is a strength.

**How to use it:** read [LEARNING_GUIDE](LEARNING_GUIDE.md) first. Then, for each section, be able to answer the eight questions without notes: *what / why / how / alternatives / trade-offs / what happens when it fails / how it scales / what I'd improve next.*

## The 60-second pitch

"NightJar is a self-hosted, OpenAI-compatible LLM gateway. You change your base URL and get response caching, optional cost routing, provider retries and limits, per-key access control, and a record of every request. The interesting part is the cache. I measured that the embedding-based cache in the original project returned wrong answers for about one in five tricky pairs, because the model scored opposite-meaning prompts higher than real paraphrases. So I redesigned it: exact matching by default, and a semantic tier that's opt-in, partitioned by model and parameters, guarded by lexical checks, with the measured false-hit rate published. I also found and fixed a denial-of-service where one large request froze the gateway for four minutes, and verified the reliability behaviour against a fake provider over real HTTP."

## Per-component walkthrough

### Exact and semantic cache (`app/cache.py`, `app/fingerprint.py`, `app/guards.py`)
- **What / why:** Serve stored answers to avoid provider calls. A wrong cached answer is worse than a miss, so correctness drives the design.
- **How:** Exact key = SHA-256 of a canonical fingerprint (model, every param including unknown ones, all messages). Semantic tier searches only within a partition (same model/params/context), thresholds similarity, then vetoes on numbers/negation/quotes/proper nouns. Only complete plain-text single-choice answers are stored.
- **Alternatives:** tune the threshold (measured: impossible, the wrong pair scored 0.992 > a true paraphrase 0.952 median); cross-encoder or LLM verification (catches more, costs an inference per lookup); per-entry learned thresholds (vCache; needs correctness feedback); no semantic tier.
- **Trade-offs:** lower hit rate than the naive design (recall 63% vs 70% at 0.92); residual false hits (about 7%, range ~2–20%, n=41) on lowercase word swaps.
- **When it fails:** Redis down → miss, request still succeeds; embedder down → exact tier only; corrupt payload → miss; dangling vector (payload expired) → removed on lookup.
- **Scale:** payloads shared via Redis; the vector index and single-flight are per process. Past ~1M vectors per partition, flat search isn't enough: Qdrant/pgvector.
- **Next:** verification step for semantic hits; per-key cache partitions; shared single-flight via Redis.

### Single-flight (`ResponseCache.begin_flight`)
- Identical concurrent requests share one provider call (tested: 8 requests → 1 call). Failure of the leader is shared, so followers don't each retry a failing provider; if the leader is *cancelled*, followers run independently. Per process only.

### Routing (`app/router.py`)
- Classification (token count + keywords) is always computed; *application* is opt-in (`ROUTING_MODE`), default honours the requested model. Decision is recorded even when not applied. **Limits:** 92.8% agreement with labels on short, keyword-aligned prompts; no quality signal; a learned router needs preference data I don't have.

### Provider layer and reliability (`app/proxy.py`, `app/limits.py`)
- LiteLLM does provider abstraction; NightJar adds policy: 60 s timeout (the library default is 6000 s), retry only 429/5xx/timeouts, auth/bad-request never retried and never trip the cooldown, concurrency cap with 503 + `Retry-After`, stream cancellation on disconnect.
- **Failure story worth telling:** in the baseline, one rejected request took the only deployment offline for 5 s ("No deployments available"). That's now a regression test.
- **Fails when:** provider down → breaker opens, fast 502s for the cooldown window (a blip becomes a short outage window; the trade is protecting the provider and latency).
- **Missing:** a retry budget; config-driven fallbacks (still in code, names possibly stale).

### Streaming (`_stream_response`)
- Await the first chunk before building the response so connect-time errors are real HTTP statuses; later errors are in-band events; upstream stream closed in `finally`; usage requested upstream (`include_usage`) and the extra chunk hidden from callers who didn't ask.

### Auth (`app/auth.py`, `app/keys.py`)
- Hashed 256-bit keys, scopes, per-key limits, three auth modes. Docker defaults to `required`. **Gap:** rate limiting is per process; no spend limits; cache not tenant-partitioned.

### Telemetry and DB (`app/telemetry.py`)
- SQLite (WAL) + versioned additive migrations; failed requests recorded; NULL for unknown tokens; write failures counted and surfaced. **Gaps:** two tables that can't be joined; cost is an estimate; no write batching.

## Questions and model answers

**1. Why is a false cache hit worse than a miss, and how did you measure yours?**
A miss costs one provider call. A false hit returns a confident wrong answer to the user and nobody notices. I built 71 labeled prompt pairs, ran the real embedding model, and counted how many should-not-match pairs each threshold would serve. At the default 0.92 it was 9 of 41 raw, 3 of 41 with guards. I also publish the caveats: pairs written by one person, guards designed alongside the categories, so the guarded number is optimistic.

**2. Why not just raise the threshold?**
Because the classes overlap. The worst wrong pair scored 0.992; the median true paraphrase scored 0.952. A threshold high enough to reject the swap rejects most real paraphrases too (recall 20% at 0.97). It's a property of the embedding model, not of the threshold.

**3. A request is blocking the whole server. How did that happen and how did you fix it?**
tiktoken's BPE is quadratic on long whitespace-free text; 200 KB took 8.6 s, and a 10 MB request pinned the process for over four minutes, including `/health`. It ran synchronously on the event loop. Fix: reject large bodies before reading them (413), cap text length, and bound exact tokenization to 8,000 characters (estimate beyond that). Re-ran the same 10 MB request: 413 in under a millisecond.

**4. How does single-flight work, and what could go wrong?**
A dict from cache key to an `asyncio.Future`. The first request is the leader; others await the future. Risks: a failing leader (followers would all retry), solved by sharing the exception; a *cancelled* leader (client hung up), solved by releasing followers to run themselves; memory leaks, solved by removing the entry in `finish`; and "exception never retrieved" warnings, handled with a done-callback. It's per process, so replicas can still duplicate work.

**5. Explain your retry and circuit-breaker behaviour.**
Retry only errors that can succeed on a second attempt: 429, 5xx, timeouts. Never 401/400. Only server-side errors count toward cooldown, three per minute; the deployment is then skipped for 10 s and requests fail fast. I tested that an open breaker doesn't call the provider and recovers afterwards. What I didn't build: a retry budget (cap retries to a fraction of traffic, per Google's SRE guidance), so under a brownout every request can still retry.

**6. What does a client see when the provider fails mid-stream?**
HTTP 200 already went out, so the status can't change. An in-band `data: {"error": ...}` event, no `[DONE]`. OpenAI SDKs raise on that event. If it fails *before* the first chunk, it's a normal 429/502.

**7. How do you attribute cost per caller?**
Requests carry the authenticated key's name into `telemetry.api_key_name`; the client-supplied `user` field is not trusted. Cost is an estimate from a short price table (0 for hits, NULL when tokens are unknown). There's also LiteLLM's `request_log` with better prices but a different request ID, so they can't be joined. I'd merge them by threading my request ID through LiteLLM's callback.

**8. Why SQLite? When would you move to Postgres?**
Single node, one small write per request, a few dashboard reads. The audit's problems weren't SQLite's fault. I'd move when I need more than one replica sharing telemetry and keys, or sustained write rates one writer can't handle. Access is isolated in two modules, so the move is contained.

**9. Why did you replace FAISS?**
I benchmarked: at 10k–100k vectors FAISS flat and numpy matrix-vector took 0.3–3 ms each. The design needs partition-filtered search, deletes and per-entry expiry, which are trivial on a numpy matrix and awkward on a flat FAISS index (the old code rebuilt the whole index per delete). The trade-off is linear scan; it stops scaling around a million vectors, and I wrote down the upgrade path.

**10. How would you scale this to multiple replicas?**
Today it doesn't: vector index, SQLite, single-flight, cooldowns and rate limits are per process. Order of work: measure first; move keys/telemetry to Postgres; keep payloads in Redis; shared vector store with filtered search; Redis-backed rate limits and single-flight. `SCALABILITY.md` has the table. I didn't build any of it because there was no multi-replica requirement to test against.

**11. How do you know your reliability tests prove anything?**
They run real HTTP to a fake provider through the real LiteLLM Router, counting what the provider actually received: a 401 is attempted once; a 500 is attempted 1+retries times; an open breaker produces zero provider calls; a saturated limiter sheds without reaching the provider; a client disconnect increments the fake's aborted-stream counter. Mocks couldn't show any of that. (And running the real stack found bugs the mocked suite couldn't: documented env var names that didn't match the settings, twice.)

**12. What's the security model, and what are you not protecting against?**
Hashed scoped keys, auth on in Docker, bounded inputs, closed CORS, no prompts in logs, fixed upstream error messages. Not covered: cache entries are shared across callers (a cached answer goes to anyone sending an identical request), no per-key spend limits, no TLS termination (reverse proxy), per-process rate limits, no audit log.

**13. How did you validate the benchmark?**
I didn't trust it blindly. The c=64 "direct" baseline was slower than going through the gateway, which is impossible, so I concluded the load generator and fake provider were saturated and marked those rows uninterpretable. I attributed the 14.5 ms overhead (0.7 ms mine, 6.5 ms LiteLLM) and said plainly that ~7 ms is unattributed.

**14. What would you do next?**
(1) Verification for semantic hits, since that decides whether the tier can ever be safe; (2) a retry budget; (3) config-driven fallbacks and current model names; (4) merge the two telemetry tables; (5) a proper load generator to find the throughput ceiling; (6) per-key cache partitioning.

## Follow-up traps (and honest answers)

- *"Is it production-ready?"* No claim of that is made. It has bounded inputs, tested failure paths and a documented security model, but it has never been run against a real provider, has no multi-replica story, and has several documented gaps.
- *"Did it save money?"* Mechanically: hits make zero provider calls (verified). In dollars: not measured, because there were no provider credentials and savings depend on your hit rate.
- *"Why LiteLLM?"* Provider adapters are large, never finished, and not what differentiates this project. The cost: ~6.5 ms per call, a big dependency, and defaults (timeout, cooldown) that needed explicit configuration.
- *"What did you get wrong?"* Several documented-vs-actual mismatches (setting names; retention default initially deleted data on upgrade, fixed to opt-in), an initial test that assumed statistical retry success, and test pollution that caused outbound calls with fake keys. Each was found by running something real and each now has a test.
