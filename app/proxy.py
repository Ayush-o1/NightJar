"""Upstream LLM forwarding logic using LiteLLM Router.

Implements:
  - LiteLLM Router for load balancing + automatic provider failover
  - Redis-backed LiteLLM Cache for exact-match response deduplication
  - Model name resolution (provider prefix auto-detection)
  - Universal tool/function-calling support with graceful translation guard
    (LiteLLM translates OpenAI tool format → provider-native format automatically)

Public API:
  ProxyClient.forward()              → non-streaming dict
  ProxyClient.forward_with_tools()  → non-streaming dict (tool-aware, with guard)
  ProxyClient.forward_stream()       → async SSE generator
  ProxyClient.simple_completion()    → dict
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import litellm
import structlog
from litellm import Router
from litellm.caching.caching import Cache

from app import telemetry as tel
from app.config import Settings
from app.models import ChatCompletionRequest

# Silence LiteLLM's default verbose logging; NightJar uses structlog
litellm.suppress_debug_info = True

logger = structlog.get_logger()


# ── LiteLLM success callback ─────────────────────────────────────────────

async def _litellm_success_callback(
    kwargs: dict,
    completion_response,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """Fires after every successful (non-cached) LiteLLM upstream call.

    Calculates cost using LiteLLM's live pricing table and writes a row
    to the request_log table in SQLite.
    """
    try:
        cost: float = 0.0
        try:
            cost = litellm.completion_cost(completion_response=completion_response)
        except Exception:
            # completion_cost() raises if the model has no pricing entry;
            # fall back to 0.0 rather than crashing the callback.
            pass

        latency_ms = (end_time - start_time).total_seconds() * 1000
        model: str = kwargs.get("model", "unknown")
        provider: str = model.split("/")[0] if "/" in model else "openai"

        usage = getattr(completion_response, "usage", None) or {}
        if hasattr(usage, "prompt_tokens"):
            prompt_tokens: int = usage.prompt_tokens or 0
            completion_tokens: int = usage.completion_tokens or 0
        else:
            prompt_tokens = (usage.get("prompt_tokens") or 0) if isinstance(usage, dict) else 0
            completion_tokens = (usage.get("completion_tokens") or 0) if isinstance(usage, dict) else 0

        record = {
            "request_id": kwargs.get("litellm_call_id") or str(uuid.uuid4()),
            "timestamp": start_time.replace(tzinfo=timezone.utc).isoformat(),
            "model": model,
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_cost": round(cost, 8),
            "user_id": kwargs.get("user"),
            "latency_ms": round(latency_ms, 2),
            "status": "success",
        }
        tel.write_request_log(record)
        logger.debug(
            "litellm.callback.cost_logged",
            model=model,
            cost_usd=round(cost, 6),
            latency_ms=round(latency_ms, 2),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("litellm.callback.error", reason=str(exc))


# Register callback immediately at module import time.
# ProxyClient.__init__ may add OTel later; this one is always active.
litellm.success_callback = [_litellm_success_callback]


# ── Known provider prefix rules ──────────────────────────────────────────

_KNOWN_PREFIXES = (
    "openai/",
    "anthropic/",
    "gemini/",
    "groq/",
    "mistral/",
    "cohere/",
    "xai/",
    "ollama/",
    "ollama_chat/",
    "huggingface/",
    "bedrock/",
    "vertex_ai/",
    "together_ai/",
    "deepinfra/",
    "replicate/",
)

_PREFIX_BY_STEM = {
    "gpt-": "openai/",
    "o1": "openai/",
    "o3": "openai/",
    "claude-": "anthropic/",
    "gemini-": "gemini/",
    "llama": "groq/",
    "mixtral": "groq/",
    "mistral": "mistral/",
    "command": "cohere/",
    "grok-": "xai/",
}

# Providers that do NOT support tool/function calling.
# LiteLLM will pass tools through but the provider returns a cryptic error;
# we intercept early and return a clear 400 instead.
_TOOL_UNSUPPORTED_PROVIDERS: frozenset[str] = frozenset({
    "ollama",
    "ollama_chat",
    "huggingface",
    "replicate",
})


def _get_litellm_model(model_name: str, settings: Settings) -> str:
    """Resolve a bare model name to a fully-qualified LiteLLM model string.

    Resolution order:
    1. Already provider-prefixed → use as-is.
    2. Matches a known model stem → auto-prefix.
    3. Infer provider from which API key is set.
    4. Pass through unchanged (LiteLLM raises if unknown).
    """
    for prefix in _KNOWN_PREFIXES:
        if model_name.startswith(prefix):
            return model_name

    for stem, prefix in _PREFIX_BY_STEM.items():
        if model_name.startswith(stem):
            return f"{prefix}{model_name}"

    if settings.groq_api_key:
        return f"groq/{model_name}"
    if settings.gemini_api_key:
        return f"gemini/{model_name}"
    if settings.mistral_api_key:
        return f"mistral/{model_name}"
    if settings.cohere_api_key:
        return f"cohere/{model_name}"
    if settings.openai_api_key:
        return f"openai/{model_name}"

    return model_name


class UpstreamError(Exception):
    """Raised when the upstream LLM API returns an error."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class ProxyClient:
    """Multi-provider LLM client backed by LiteLLM Router.

    The Router provides:
    - Automatic failover: if gpt-4o hits a 429, falls back to claude/gemini
    - Load balancing across deployments (simple-shuffle strategy)
    - Configurable retries before escalating to fallback providers

    LiteLLM Cache (Redis) provides:
    - Exact-match caching: identical prompts skip the API entirely
    - Complements NightJar's own semantic cache (FAISS) which catches
      near-duplicate queries; this layer catches byte-identical repeats
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._configure_environment(settings)

        # ── Build the Router model list + fallback chains ────────────────
        model_list = self._build_model_list(settings)
        fallbacks = self._build_fallback_map(settings)

        self.router = Router(
            model_list=model_list,
            fallbacks=fallbacks,
            num_retries=2,
            retry_after=0,                  # don't wait between retries in tests
            routing_strategy="simple-shuffle",
            set_verbose=False,
        )

        # ── Enable LiteLLM Redis cache (optional) ────────────────────────
        if settings.enable_cache:
            self._enable_cache(settings)

        logger.info(
            "proxy.router.initialized",
            deployments=len(model_list),
            fallbacks=len(fallbacks),
            cache_enabled=settings.enable_cache,
        )

    # ── Static builders ──────────────────────────────────────────────────

    @staticmethod
    def _build_model_list(settings: Settings) -> list[dict]:
        """Build the LiteLLM Router deployment list from available credentials.

        Each entry maps a logical model_name (e.g. "simple-tier", "gpt-4o")
        to a concrete provider+model pair. Multiple entries with the same
        model_name form a load-balanced pool; on failure the Router tries the
        next entry in the fallback chain.
        """
        entries: list[dict] = []

        def add(model_name: str, litellm_model: str, api_key: str | None = None) -> None:
            params: dict = {"model": litellm_model}
            if api_key:
                params["api_key"] = api_key
            entries.append({"model_name": model_name, "litellm_params": params})

        # ── Simple tier ─────
        if settings.openai_api_key:
            add("simple-tier", "openai/gpt-3.5-turbo", settings.openai_api_key)
            add("gpt-3.5-turbo", "openai/gpt-3.5-turbo", settings.openai_api_key)
        if settings.groq_api_key:
            add("simple-tier", "groq/llama3-8b-8192", settings.groq_api_key)
            add("groq/llama3-8b-8192", "groq/llama3-8b-8192", settings.groq_api_key)
        if settings.gemini_api_key:
            add("simple-tier", "gemini/gemini-2.0-flash", settings.gemini_api_key)
            add("gemini-2.0-flash", "gemini/gemini-2.0-flash", settings.gemini_api_key)

        # ── Complex tier ─────
        if settings.openai_api_key:
            add("complex-tier", "openai/gpt-4o", settings.openai_api_key)
            add("gpt-4o", "openai/gpt-4o", settings.openai_api_key)
        if settings.anthropic_api_key:
            add("complex-tier", "anthropic/claude-3-5-sonnet-20241022", settings.anthropic_api_key)
            add("claude-3-5-sonnet", "anthropic/claude-3-5-sonnet-20241022", settings.anthropic_api_key)
        if settings.gemini_api_key:
            add("complex-tier", "gemini/gemini-2.5-pro-preview-03-25", settings.gemini_api_key)
            add("gemini-2.5-pro", "gemini/gemini-2.5-pro-preview-03-25", settings.gemini_api_key)

        # ── Ensure at least one entry so Router doesn't fail on init ─────
        if not entries:
            entries.append({
                "model_name": "default",
                "litellm_params": {"model": "openai/gpt-3.5-turbo"},
            })

        return entries

    @staticmethod
    def _build_fallback_map(settings: Settings) -> list[dict]:
        """Define explicit fallback chains for critical model groups.

        Format: [{"primary-model-name": ["fallback1", "fallback2"]}, ...]
        LiteLLM Router uses these when the primary deployment fails after
        num_retries exhaustion.
        """
        fallbacks: list[dict] = []

        # gpt-4o → claude → gemini
        gpt4o_fallbacks: list[str] = []
        if settings.anthropic_api_key:
            gpt4o_fallbacks.append("claude-3-5-sonnet")
        if settings.gemini_api_key:
            gpt4o_fallbacks.append("gemini-2.5-pro")
        if gpt4o_fallbacks:
            fallbacks.append({"gpt-4o": gpt4o_fallbacks})
            fallbacks.append({"complex-tier": gpt4o_fallbacks})

        # gpt-3.5-turbo → groq/llama3 → gemini-flash
        gpt35_fallbacks: list[str] = []
        if settings.groq_api_key:
            gpt35_fallbacks.append("groq/llama3-8b-8192")
        if settings.gemini_api_key:
            gpt35_fallbacks.append("gemini-2.0-flash")
        if gpt35_fallbacks:
            fallbacks.append({"gpt-3.5-turbo": gpt35_fallbacks})
            fallbacks.append({"simple-tier": gpt35_fallbacks})

        return fallbacks

    @staticmethod
    def _enable_cache(settings: Settings) -> None:
        """Attach a Redis-backed LiteLLM cache; degrade gracefully if unavailable."""
        try:
            litellm.cache = Cache(
                type="redis",
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                ttl=settings.cache_ttl_seconds,
            )
            logger.info(
                "proxy.cache.enabled",
                host=settings.redis_host,
                port=settings.redis_port,
                ttl=settings.cache_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("proxy.cache.unavailable", reason=str(exc))
            litellm.cache = None

    @staticmethod
    def _configure_environment(settings: Settings) -> None:
        """Map settings values into os.environ so LiteLLM providers find them."""
        key_map = {
            "OPENAI_API_KEY": settings.openai_api_key,
            "ANTHROPIC_API_KEY": settings.anthropic_api_key,
            "GEMINI_API_KEY": settings.gemini_api_key,
            "GROQ_API_KEY": settings.groq_api_key,
            "MISTRAL_API_KEY": settings.mistral_api_key,
            "COHERE_API_KEY": settings.cohere_api_key,
            "XAI_API_KEY": settings.xai_api_key,
        }
        for env_var, value in key_map.items():
            if value:
                os.environ.setdefault(env_var, value)

        if settings.ollama_base_url and settings.ollama_base_url != "http://localhost:11434":
            os.environ.setdefault("OLLAMA_API_BASE", settings.ollama_base_url)

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _map_error(exc: Exception) -> UpstreamError:
        """Map any LiteLLM / provider exception to UpstreamError."""
        status = getattr(exc, "status_code", None) or 500
        return UpstreamError(status_code=int(status), detail=str(exc))

    def _resolve_model(self, model_name: str) -> str:
        """Resolve a model name for the Router.

        Prefers registered model_names (gets failover + balancing) over
        the raw LiteLLM prefix resolver. Falls back to prefix resolution
        for arbitrary user-supplied model strings.
        """
        registered = {entry["model_name"] for entry in self.router.model_list}
        if model_name in registered:
            return model_name
        return _get_litellm_model(model_name, self.settings)

    # ── Public API ───────────────────────────────────────────────────────

    async def forward(
        self, request: ChatCompletionRequest, model_override: str | None = None
    ) -> dict:
        """Forward a non-streaming request; returns raw OpenAI-compatible dict.

        Uses self.router.acompletion so the Router's failover and retry logic
        applies automatically.
        """
        try:
            payload = request.model_dump(exclude_none=True)
            payload["stream"] = False
            raw_model = model_override or payload.pop("model", request.model)
            payload["model"] = self._resolve_model(raw_model)

            response = await self.router.acompletion(**payload)
            return response.model_dump()

        except UpstreamError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def forward_stream(
        self, request: ChatCompletionRequest, model_override: str | None = None
    ) -> AsyncGenerator[str, None]:
        """Forward a streaming request; yields SSE-formatted data strings."""
        try:
            payload = request.model_dump(exclude_none=True)
            payload["stream"] = True
            raw_model = model_override or payload.pop("model", request.model)
            payload["model"] = self._resolve_model(raw_model)

            stream = await self.router.acompletion(**payload)

            async for chunk in stream:
                yield f"data: {json.dumps(chunk.model_dump())}\n\n"

            yield "data: [DONE]\n\n"

        except UpstreamError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def simple_completion(
        self,
        messages: list,
        model: str,
        temperature: float = 0.7,
        **kwargs,
    ) -> dict:
        """Lightweight completion for internal use (e.g. context compressor).

        Returns a dict with OpenAI-style response structure so callers can do:
            response["choices"][0]["message"]["content"]

        Also includes a ``compression_metadata`` key with token counts for the
        summarisation request, enabling callers to log accurate before/after stats:
            response["compression_metadata"]["prompt_tokens"]
            response["compression_metadata"]["completion_tokens"]
        """
        try:
            resolved = self._resolve_model(model)
            response = await self.router.acompletion(
                model=resolved,
                messages=messages,
                temperature=temperature,
                max_tokens=kwargs.pop("max_tokens", 500),
                **kwargs,
            )
            result = response.model_dump()
            usage = result.get("usage") or {}
            result["compression_metadata"] = {
                "model": resolved,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            return result

        except UpstreamError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def forward_with_tools(
        self,
        request: ChatCompletionRequest,
        model_override: str | None = None,
    ) -> dict:
        """Forward a request that includes OpenAI-format tools/function definitions.

        Translation Guard
        -----------------
        LiteLLM automatically translates the OpenAI ``tools`` format into each
        provider's native format (Anthropic ``tools``, Gemini ``functionDeclarations``,
        etc.). If the resolved model belongs to a provider that does NOT support
        tool calling, this method raises an UpstreamError(400) with a clear message
        rather than letting the provider return a cryptic error.

        Providers with known tool support: OpenAI, Anthropic, Gemini, Mistral,
        Cohere, Groq (llama3-70b and newer). Ollama and some older Groq models
        do not support tools.
        """
        payload = request.model_dump(exclude_none=True)
        payload["stream"] = False
        raw_model = model_override or payload.pop("model", request.model)
        resolved = self._resolve_model(raw_model)
        payload["model"] = resolved

        # Translation guard — check before making the upstream call
        tools = payload.get("tools")
        if tools:
            provider = resolved.split("/")[0] if "/" in resolved else "openai"
            if provider in _TOOL_UNSUPPORTED_PROVIDERS:
                raise UpstreamError(
                    status_code=400,
                    detail=(
                        f"Provider '{provider}' does not support tool/function calling. "
                        f"Use a tool-capable model (openai, anthropic, gemini, mistral, "
                        f"groq/llama3-70b-8192) or omit the 'tools' parameter."
                    ),
                )

        try:
            response = await self.router.acompletion(**payload)
            return response.model_dump()
        except UpstreamError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

    async def close(self) -> None:
        """No-op — LiteLLM Router manages its own connection pool."""
