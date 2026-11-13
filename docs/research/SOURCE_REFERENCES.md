# Source references

Every external claim in `docs/` that influenced a design decision is listed here with how well I was able to check it. Research was done on 2026-11-13 with web search plus direct page fetches.

**Confidence column**

- **Fetched**: I read the page itself and the claim is from its text.
- **Search summary**: the claim comes from a search-result summary of the page, not from me reading it. Treat as plausible, not verified.
- **Not verified**: I could not confirm it; the doc that uses it says so.

| # | Source | Used for | Confidence |
|---|---|---|---|
| 1 | LiteLLM, [Router reliability: retries, fallbacks, cooldowns](https://docs.litellm.ai/docs/router/learn/reliability) | Retries stay inside a model group; fallbacks move between groups; cooldown removes a deployment after `allowed_fails`; `num_retries` default 2; `max_fallbacks` default 5 | Search summary |
| 2 | LiteLLM, [Router configuration reference](https://docs.litellm.ai/docs/router/api_reference/configuration) | Router parameters (`retry_policy`, `allowed_fails_policy`, `cooldown_time`) | Search summary. Parameter names are also checked against the installed litellm 1.95.1 in Phase 7. |
| 3 | Redis, [LangCache concepts](https://redis.io/docs/latest/develop/ai/context-engine/langcache/concepts/) | Redis's own semantic cache defaults to a 0.85 threshold, recommends starting 0.8–0.9 and tuning on your own data; says a tighter threshold reduces wrong-answer risk | Search summary |
| 4 | Redis, [10 techniques for semantic cache optimization](https://redis.io/blog/10-techniques-for-semantic-cache-optimization/) | Start with a high threshold and lower gradually | Search summary |
| 5 | Gill et al., [MeanCache (arXiv 2403.02694)](https://arxiv.org/pdf/2403.02694) | GPTCache's default 0.7 threshold gave many false hits on unique queries (233 of 700 in their test); best threshold depends on the embedding model | Search summary. Setup-specific, may not transfer. |
| 6 | Schroeder et al., [vCache (arXiv 2502.03771)](https://arxiv.org/pdf/2502.03771) | Argues a single static threshold across all prompts is the core flaw; proposes per-entry learned thresholds with an error-rate bound | Search summary |
| 7 | Ong et al., [RouteLLM (arXiv 2406.18665)](https://arxiv.org/abs/2406.18665v4) | Routers need quality evidence (preference data), judged by cost vs. performance-gap recovered; reported >2x cost cut on the paper's benchmarks | Search summary |
| 8 | Google, [SRE Book ch. 21 Handling Overload](https://sre.google/sre-book/handling-overload/) | Per-request retry cap (3) and per-client retry budget (~10% of traffic) to avoid retry amplification; client-side adaptive throttling | Search summary. The page was not read; specific figures came from secondary write-ups. |
| 9 | OpenAI, [Error codes guide](https://developers.openai.com/api/docs/guides/error-codes) | 401 for invalid key, 429 for rate limit with `Retry-After` guidance | **Fetched** |
| 10 | OpenAI, [Create chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) and [streaming events](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events) | `stream_options.include_usage` adds a final chunk with token usage before `data: [DONE]`; usage chunk may be absent if the stream is interrupted | **Fetched** |
| 11 | OpenAI error object `{error:{message,type,param,code}}` | Shape for NightJar's error responses | **Not verified** on an official page (the error-codes page names `type`, `code`, `param` in passing). It is the shape the OpenAI SDKs and third-party gateways use; NightJar follows it and says so. |
| 12 | Starlette, [Middleware docs](https://www.starlette.io/middleware/) | `BaseHTTPMiddleware` has known limitations; pure ASGI middleware recommended for streaming | **Not verified.** Page could not be fetched (DNS failure from the research environment). Backed only by search summaries of the docs and issue threads. NightJar uses pure ASGI middleware regardless, since it is simple to test directly. |
| 13 | Portkey, [AI Gateway repository](https://github.com/Portkey-AI/gateway) and [docs](https://portkey.ai/docs/product/ai-gateway-streamline-llm-integrations) | Config-driven retries, fallbacks, load balancing; hooks | Search summary; vendor-stated |
| 14 | Helicone, [LLM gateways comparison](https://www.helicone.ai/blog/top-llm-gateways-comparison-2025) | Landscape positioning only | Search summary; vendor-authored, treated as marketing. Search results also suggested Helicone's project status may have changed; not verified. |
| 15 | OWASP, [Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) | LLM02 Sensitive Information Disclosure, LLM10 Unbounded Consumption ("denial of wallet") frame the security model | Search summary from secondary blogs; the OWASP page itself was not read |
| 16 | Local measurements in [BASELINE.md](../BASELINE.md) | tiktoken quadratic behavior, embedder false hits, router accuracy | **Measured here** |

Not researched in depth, noted so nobody assumes otherwise: Langfuse, Kong AI Gateway, OpenRouter and vLLM. They are named in the landscape doc from search summaries only, and no design decision depends on them.
