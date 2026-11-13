# Landscape

What else exists, and where NightJar fits. Sources and their reliability: [SOURCE_REFERENCES.md](SOURCE_REFERENCES.md). Most comparison material found online is written by vendors, so this is deliberately short and limited to what influenced a decision.

## The categories

| Kind | Examples | What it does | Relation to NightJar |
|---|---|---|---|
| Provider-unification library / proxy | LiteLLM | One call signature across providers; Router with retries, fallbacks, cooldowns; optional proxy server with keys and budgets | NightJar **uses** LiteLLM as its provider layer. Reimplementing 100+ provider adapters would be pointless. |
| Gateway with config-driven policies | Portkey (open-source gateway), Kong AI Gateway | Retries, fallbacks, load balancing, guardrails, auth, caching declared in config | Same space. NightJar's differentiator has to be elsewhere (below), not "also has fallbacks". |
| Hosted router / marketplace | OpenRouter | One endpoint and one bill across many models, provider fallback | Hosted; different trade (billing, data leaves your network). |
| Observability layer | Langfuse, Helicone | Traces, cost, evals, prompt management; Helicone can also sit in the request path | NightJar's telemetry is request-level only. It does not try to replace trace/eval tooling. Optional OpenTelemetry export is the integration point. |
| Semantic-cache products | Redis LangCache, GPTCache | Embed prompts, return stored responses above a similarity threshold | NightJar's cache lives in this category. See the cache findings. |
| Serving engines | vLLM | Run models locally | Not a gateway. NightJar can sit in front of one through LiteLLM's OpenAI-compatible provider. |

## Where NightJar can be useful instead of redundant

LiteLLM's proxy already covers a lot of the gateway checklist (keys, budgets, fallbacks). Competing with it on breadth would lose. The part of the current project that is **not** covered by simply running LiteLLM is the cost-reduction pipeline:

1. a **cache that is explicit about when it is allowed to answer** (exact match always; semantic only under guards, with measured false-hit numbers), and
2. **opt-in cost routing** with a decision trail you can inspect per request,
3. in one small, self-hostable service whose behavior you can read in an afternoon, with per-request records of what happened and why.

Those are scoped as the product. Provider failover, rate limiting, auth and telemetry are table-stakes plumbing that must be correct, not features to market.

## What the research said about each pillar

**Reliability.** LiteLLM's Router already provides retries within a model group, fallbacks across groups, and cooldowns that take a failing deployment out of rotation (it does not use the term "circuit breaker" [1]). NightJar does not reimplement these. It configures them deliberately (the baseline showed that default cooldown behavior can take a single-deployment group offline after one rejected request) and adds what the Router lacks at the gateway level: request deadlines, a bounded retry budget, and concurrency limits. The retry-budget idea comes from Google's SRE handling-overload chapter [8].

**Caching.** Redis recommends a 0.8–0.9 starting threshold and tuning against your own queries [3]; MeanCache reports a 0.7 default producing many false hits [5]; vCache argues a single static threshold is the fundamental flaw [6]. My own measurement agrees (BASELINE.md): with the shipped embedder, some opposite-meaning prompts score above true paraphrases. That moves the design from "tune the threshold" to "partition and gate".

**Routing.** RouteLLM [7] treats routing as a learned, quality-evaluated decision and evaluates with cost-versus-quality curves. NightJar's keyword router has no quality signal at all. The honest conclusions: keep rules because they are explainable and cheap, never switch models for a caller who asked for a specific one unless they opted in, and measure with a labeled set that includes prompts where cheap models fail. A learned router is not justified without quality labels, so it is out of scope.

**Streaming and middleware.** OpenAI documents `stream_options.include_usage`, which adds a final chunk carrying token usage [10]. That is how streaming requests can be costed without counting tokens by hand. The usage chunk may be missing if a stream is cut off, so the telemetry has to record "unknown" in that case rather than zero.

**Security.** OWASP's 2025 LLM list names sensitive-information disclosure (LLM02) and unbounded consumption (LLM10) [15]. For a gateway that means: don't log prompts by default, don't echo provider errors blindly, and bound size, rate and spend per key. The baseline's 10 MB request is a live example of LLM10.

## Non-goals

- Replacing LiteLLM, or writing provider adapters.
- A learned routing model.
- Prompt management, evals, or trace UIs (Langfuse territory).
- A multi-region or multi-tenant SaaS control plane.
- Guardrails/content moderation.
