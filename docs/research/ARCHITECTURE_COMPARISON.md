# Architecture comparison

How comparable systems structure the same concerns, and what NightJar takes or leaves. The cells for third-party systems come from search summaries of their own docs ([SOURCE_REFERENCES.md](SOURCE_REFERENCES.md), rows 1, 2, 13, 14), so read them as "as documented by the vendor", not as tested behavior.

| Concern | LiteLLM (Router/proxy) | Portkey open-source gateway | Redis LangCache | NightJar (target) |
|---|---|---|---|---|
| Deployment shape | Library + optional proxy server | Standalone TypeScript gateway process | Managed service (preview on Redis Cloud) | One Python service + Redis, SQLite file |
| Provider abstraction | Own adapters for many providers | Own adapters, one API | n/a | **Delegated to LiteLLM**; NightJar adds no adapters |
| Routing | Load balancing across deployments; strategies | Config-driven: weighted load balance, conditional routing | n/a | Rule-based tier routing, opt-in via alias |
| Retries / fallbacks | `num_retries`, fallbacks across groups, `retry_policy` | Retries with backoff, fallbacks in config | n/a | LiteLLM Router configured explicitly + retry budget |
| Failure isolation | Cooldown removes a failing deployment after `allowed_fails` | Per-target config | n/a | Router cooldown with error-class policy + concurrency limit |
| Caching | Exact-match (Redis/in-memory) and a semantic cache option | Simple and semantic cache modes | Semantic, default threshold 0.85, tune on own data | Exact tier default; semantic tier gated and opt-in |
| State sharing | In-memory + optional Redis routing cache for multi-worker | Stateless gateway | Redis | Cache in Redis; FAISS and SQLite are per-instance (documented limit) |
| Auth / keys | Virtual keys, budgets (proxy) | Virtual keys, guardrails (varies by edition) | Service API keys | Hashed per-key records with limits |
| Observability | Callbacks (many sinks), OTel | Logs/analytics | Cache analytics | One `requests` table, `/metrics`-style summary endpoint, optional OTel |

## Decisions that come out of the comparison

**Don't write provider adapters.** Every system above either owns that code (large, never-ending maintenance) or delegates it. A student-scale project that reimplemented it would spend its time on the least differentiating part.

**Don't build a second gateway inside the first.** LiteLLM's proxy already provides virtual keys and budgets. NightJar keeps its own small key table because it needs request-level attribution for its cache and routing decisions, and it stays minimal rather than competing on breadth.

**Modular monolith, not services.** None of the compared systems need a distributed deployment for the single-node case, and the audit's problems have nothing to do with process boundaries. The Redis boundary already exists. Splitting the embedder or router into services would add network hops to a path whose entire purpose is lower latency. The measured reason to distribute would be CPU saturation from embedding; the design supports moving embedding to a worker pool inside the process first, and a separate service only if Phase 11 shows that is the bottleneck.

**Share state where it already is.** Multi-replica scaling is blocked by FAISS and SQLite being local. Two upgrade paths, both documented in [../architecture/SCALABILITY.md](../architecture/SCALABILITY.md): (a) sticky routing by tenant to per-node caches, or (b) move vectors to a shared store and telemetry to Postgres. Neither is built now, because there is no multi-replica requirement to validate against.

**Telemetry stays request-level.** Langfuse and Helicone cover traces, evals and prompt management. NightJar records what the *gateway* decided and what it cost, and exports OpenTelemetry for people who want the rest.
