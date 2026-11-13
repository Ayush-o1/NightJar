"""FastAPI application entry point for NightJar."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app import __version__
from app import telemetry as tel
from app.adaptive import ThresholdManager, get_active_threshold
from app.api.admin import router as admin_router
from app.asgi import RequestContextMiddleware
from app.auth import require_api_key
from app.cache import SemanticCache
from app.compat import get_header
from app.compressor import compress_context
from app.config import Settings, get_settings
from app.embedder import Embedder
from app.errors import (
    NightJarError,
    context_too_large,
    from_upstream,
    install_error_handlers,
    redact_secrets,
    sse_error_event,
)
from app.middleware import TelemetryMiddleware
from app.models import ChatCompletionRequest, HealthResponse
from app.proxy import ProxyClient, UpstreamError
from app.router import ModelRouter
from app.tokens import total_text_chars
from app.vector_store import VectorStore

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle — initialize and teardown resources."""
    settings: Settings = get_settings()

    # --- OpenTelemetry (opt-in) ---
    if settings.enable_otel:
        try:
            from opentelemetry import trace  # type: ignore[import-untyped]
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-untyped]
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-untyped]
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-untyped]

            _provider = TracerProvider()
            _provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
            )
            trace.set_tracer_provider(_provider)
            import litellm as _litellm
            if "otel" not in (_litellm.callbacks or []):
                _litellm.callbacks = list(_litellm.callbacks or []) + ["otel"]
            logger.info("otel.initialized", endpoint=settings.otel_endpoint)
        except ImportError:
            logger.warning(
                "otel.sdk_not_installed",
                hint="pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc",
            )

    # --- Proxy client ---
    proxy_client = ProxyClient(settings)
    application.state.proxy_client = proxy_client
    application.state.settings = settings

    # --- Embedding model ---
    embedder = Embedder()
    application.state.embedder = embedder

    # --- Vector store (FAISS) ---
    vector_store = VectorStore(
        dimension=embedder.dimension,
        index_path=settings.faiss_index_path,
    )
    application.state.vector_store = vector_store

    # --- Redis ---
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    application.state.redis = redis_client

    # --- Semantic cache ---
    cache = SemanticCache(
        embedder=embedder,
        vector_store=vector_store,
        redis=redis_client,
        settings=settings,
    )
    application.state.cache = cache

    # --- Model router (with test_mode support) ---
    router = ModelRouter(
        config_path="config/routing_rules.yaml",
        preferred_provider=settings.preferred_provider,
        test_mode=settings.test_mode,
        simple_model=settings.simple_model,
        complex_model=settings.complex_model,
        routing_mode=settings.routing_mode,
    )
    application.state.router = router

    # --- Telemetry DB ---
    tel.init_db()

    # --- Adaptive threshold manager ---
    threshold_manager = ThresholdManager(db_path=settings.sqlite_db_path)
    application.state.threshold_manager = threshold_manager

    for name in settings.deprecated_settings_in_use:
        logger.warning("deprecated.setting", setting=name, use_instead="NIGHTJAR_API_KEYS")
    logger.info("nightjar.started", version=__version__, log_level=settings.log_level, test_mode=settings.test_mode)
    yield

    # --- Shutdown ---
    await cache.close()
    await proxy_client.close()
    logger.info("nightjar.shutdown")


app = FastAPI(
    title="NightJar",
    description="Proxy middleware for LLM-powered apps — semantic caching, smart model routing, context compression.",
    version=__version__,
    lifespan=lifespan,
)

# ─── Middleware (order matters: first added = outermost) ─────────────────
# TelemetryMiddleware MUST be registered to write per-request telemetry.
# Added first => innermost; RequestContextMiddleware (added last) is outermost.
app.add_middleware(TelemetryMiddleware)

_settings_for_cors = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings_for_cors.cors_origins,
    # Never combine wildcard/browser-open origins with credentialed
    # (cookie) CORS — this gateway authenticates via bearer token
    # (see app/auth.py), which doesn't need allow_credentials at all.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost: assigns the request id, enforces the body limit, catches anything unhandled.
app.add_middleware(RequestContextMiddleware, max_body_bytes=_settings_for_cors.max_request_bytes)
install_error_handlers(app)

# ─── Mount static dashboard at /dashboard ────────────────────────────────
_dashboard_dir = Path(__file__).resolve().parent.parent / "docs" / "dashboard"
if _dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")

# ─── Admin router (cost reporting, request log) ──────────────────────────
app.include_router(admin_router, dependencies=[Depends(require_api_key)])


# ───────────────────────── Health Check ──────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    """Health check endpoint.

    Always returns 200 with status="ok" as long as the process is serving
    requests — Redis is an optional dependency (the semantic cache degrades
    to pass-through on failure, see app/cache.py), so its outage alone
    shouldn't fail readiness/liveness probes. The ``redis`` field reports
    that dependency's reachability separately for diagnostics.
    """
    redis_status = "unknown"
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is not None:
        try:
            await redis_client.ping()
            redis_status = "ok"
        except Exception:  # noqa: BLE001
            redis_status = "unreachable"
    return HealthResponse(redis=redis_status)


# ─────────────────── Chat Completions Endpoint ───────────────────────────


@app.post("/v1/chat/completions", response_model=None, dependencies=[Depends(require_api_key)])
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint.

    Pipeline:
      1. Route model (classify complexity → select tier)
      2. Compress context if conversation is long
      3. Check semantic cache for a similar prompt
      4. On cache hit → return cached response immediately
      5. On cache miss → forward to upstream, cache the result
    Streaming requests bypass cache and compression.
    """
    proxy_client: ProxyClient = request.app.state.proxy_client
    cache: SemanticCache = request.app.state.cache
    router: ModelRouter = request.app.state.router
    settings: Settings = request.app.state.settings
    threshold_manager: ThresholdManager | None = getattr(request.app.state, "threshold_manager", None)

    # Set before the try block so TelemetryMiddleware still records which
    # model was requested even if routing/compression/cache/upstream fails
    # below — otherwise failed requests were logged with model_requested=None,
    # which made cost/error telemetry for a specific model unreliable.
    request.state.model_requested = body.model

    try:
        messages_dicts = [m.model_dump(exclude_none=True) for m in body.messages]

        text_chars = total_text_chars(messages_dicts)
        if text_chars > settings.max_prompt_chars:
            raise context_too_large(text_chars, settings.max_prompt_chars)

        # --- Model routing ---
        override_model = get_header(request, "model-override")
        _enforce_model_policy(settings, body.model, override_model)
        routing = router.route(body.model, messages_dicts, override_model=override_model)
        request.state.model_used = routing.model_selected
        request.state.tier = routing.tier.value
        request.state.routing_reason = routing.reason

        # Streaming bypasses cache and compression
        if body.stream:
            return await _stream_response(proxy_client, body, routing)

        # --- Context compression ---
        no_compress = get_header(request, "no-compress") == "true"
        compression_ratio = 1.0
        compressed_messages = messages_dicts

        if not no_compress:
            compressed_messages, compression_ratio = await compress_context(
                messages_dicts,
                body.model,
                proxy_client,
                request.app.state.settings,
            )
            # Update body.messages so forward() sends compressed messages upstream
            body.messages = [
                body.messages[0].__class__(**m) for m in compressed_messages
            ]

        # --- Semantic cache lookup (use adaptive threshold) ---
        active_threshold = get_active_threshold(settings, threshold_manager)
        cache_result = await cache.lookup(body.model, compressed_messages, threshold=active_threshold)

        if cache_result.hit:
            # --- Telemetry: cache hit ---
            request.state.cache_hit = True
            request.state.similarity_score = cache_result.similarity_score
            request.state.prompt_tokens = 0
            request.state.completion_tokens = 0
            request.state.compressed = not no_compress
            request.state.compression_ratio = compression_ratio

            return JSONResponse(
                content=cache_result.response,
                headers={
                    "X-Cache": "HIT",
                    "X-Similarity": str(cache_result.similarity_score),
                    "X-Model-Tier": routing.tier.value,
                    "X-Model-Selected": routing.model_selected,
                    "X-Compressed": str(not no_compress),
                    "X-Compression-Ratio": str(compression_ratio),
                },
            )

        # --- Cache miss: forward upstream with routed model ---
        response_data = await proxy_client.forward(body, model_override=routing.model_selected)

        # Store in cache
        await cache.store(body.model, compressed_messages, response_data)

        # --- Telemetry: cache miss ---
        usage = response_data.get("usage") or {}
        request.state.cache_hit = False
        request.state.similarity_score = None
        request.state.prompt_tokens = usage.get("prompt_tokens", 0)
        request.state.completion_tokens = usage.get("completion_tokens", 0)
        request.state.compressed = not no_compress
        request.state.compression_ratio = compression_ratio

        return JSONResponse(
            content=response_data,
            headers={
                "X-Cache": "MISS",
                "X-Model-Tier": routing.tier.value,
                "X-Model-Selected": routing.model_selected,
                "X-Compressed": str(not no_compress),
                "X-Compression-Ratio": str(compression_ratio),
            },
        )

    except UpstreamError as exc:
        logger.warning(
            "upstream.error", request_id=getattr(request.state, "request_id", None),
            status_code=exc.status_code, detail=redact_secrets(exc.detail)[:300],
        )
        raise from_upstream(exc.status_code, exc.detail) from exc


def _enforce_model_policy(settings: Settings, requested: str, override: str | None) -> None:
    """Reject caller-chosen models outside ALLOWED_MODELS (when set).

    Applies to the requested model and to the override header. The routing alias and the
    operator-configured tier models are always allowed. This bounds which models a caller
    can spend money on; per-key limits come with the access-control phase.
    """
    allowed = settings.allowed_model_list
    if not allowed:
        return
    from app.router import ROUTING_ALIAS

    for name, param in ((override, "X-NightJar-Model-Override"), (requested, "model")):
        if name and name != ROUTING_ALIAS and name not in allowed:
            raise NightJarError(
                403, f"Model {name!r} is not allowed on this gateway.",
                type="permission_error", code="model_not_allowed", param=param,
            )


async def _stream_response(proxy_client: ProxyClient, body: ChatCompletionRequest, routing) -> StreamingResponse:
    """Start the upstream stream, then hand back an SSE response.

    The first chunk is awaited *before* the response is created. Failures that happen while
    connecting (bad key, unknown model, rate limit, cooldown) therefore still produce a real
    HTTP error status. Once bytes are on the wire the status can't change, so a later failure
    is sent in-band as an ``error`` event, which the OpenAI SDKs raise as an exception.
    """
    stream = proxy_client.forward_stream(body, model_override=routing.model_selected)
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        first = None
    except UpstreamError as exc:
        raise from_upstream(exc.status_code, exc.detail) from exc

    async def events() -> AsyncGenerator[str, None]:
        try:
            if first is not None:
                yield first
            async for chunk in stream:
                yield chunk
        except UpstreamError as exc:
            yield sse_error_event(from_upstream(exc.status_code, exc.detail))
        except Exception as exc:  # noqa: BLE001 - nothing else can be done once streaming started
            logger.error("stream.interrupted", reason=redact_secrets(str(exc))[:300])
            yield sse_error_event(NightJarError(
                502, "The upstream stream was interrupted.", type="upstream_error", code="stream_interrupted",
            ))
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Model-Tier": routing.tier.value,
            "X-Model-Selected": routing.model_selected,
        },
    )


# ─────────────────── Telemetry Endpoints ─────────────────────────────────


@app.get("/v1/telemetry", dependencies=[Depends(require_api_key)])
async def get_telemetry(limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    return {"records": tel.get_records(limit, offset), "limit": limit, "offset": offset}


@app.get("/v1/telemetry/summary", dependencies=[Depends(require_api_key)])
async def get_telemetry_summary():
    return tel.get_summary()


# ────────────────── Adaptive Threshold Endpoints ─────────────────────────


@app.get("/v1/threshold", dependencies=[Depends(require_api_key)])
async def get_threshold(request: Request):
    """Return the current adaptive threshold info."""
    settings: Settings = request.app.state.settings
    manager: ThresholdManager = request.app.state.threshold_manager
    return manager.get_info(settings)


@app.get("/v1/threshold/history", dependencies=[Depends(require_api_key)])
async def get_threshold_history(request: Request, limit: int = Query(default=20, ge=1, le=200)):
    """Return recent adaptive-threshold evaluations, newest first."""
    manager: ThresholdManager = request.app.state.threshold_manager
    return {"records": manager.get_history(limit)}


@app.post("/v1/threshold/evaluate", dependencies=[Depends(require_api_key)])
async def evaluate_threshold(request: Request):
    """Manually trigger an adaptive threshold evaluation."""
    settings: Settings = request.app.state.settings
    manager: ThresholdManager = request.app.state.threshold_manager
    result = manager.evaluate(settings)
    return result


# ─────────────────── Cache Invalidation Endpoints ────────────────────────


@app.get("/v1/cache/stats", dependencies=[Depends(require_api_key)])
async def cache_stats(request: Request):
    """Return cache statistics."""
    settings: Settings = request.app.state.settings
    cache: SemanticCache = request.app.state.cache
    threshold_manager: ThresholdManager | None = getattr(request.app.state, "threshold_manager", None)
    stats = await cache.stats()
    stats["similarity_threshold"] = get_active_threshold(settings, threshold_manager)
    return stats


@app.delete("/v1/cache", dependencies=[Depends(require_api_key)])
async def flush_cache(request: Request):
    """Flush the entire semantic cache."""
    cache: SemanticCache = request.app.state.cache
    result = await cache.flush()
    return {"status": "ok", **result}


@app.delete("/v1/cache/{key}", dependencies=[Depends(require_api_key)])
async def invalidate_cache_key(key: str, request: Request):
    """Invalidate a specific cache entry by key."""
    cache: SemanticCache = request.app.state.cache
    removed = await cache.invalidate(key)
    return {"status": "ok", "key": key, "removed": removed}
