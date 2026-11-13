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
from app.api.admin import router as admin_router
from app.asgi import RequestContextMiddleware
from app.auth import require_api_key
from app.cache import FlightAborted, ResponseCache
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
from app.fingerprint import build_fingerprint
from app.limits import ConcurrencyLimiter
from app.middleware import TelemetryMiddleware
from app.models import ChatCompletionRequest, ChatMessage, HealthResponse
from app.proxy import ProxyClient, UpstreamError
from app.router import ModelRouter
from app.tokens import total_text_chars
from app.vector_index import VectorIndex

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

    # --- Embedding model: only loaded when the semantic tier is on (it is the heavy part) ---
    embedder = Embedder() if settings.semantic_cache else None
    application.state.embedder = embedder

    # --- Vector index (in-process; persisted to a local file) ---
    index = VectorIndex(
        dimension=embedder.dimension if embedder else 384,
        max_entries=settings.cache_max_entries,
    )
    if embedder:
        logger.info("vector_index.loaded", entries=index.load(settings.vector_index_path))
    application.state.vector_index = index

    # --- Redis (short timeouts: a hung Redis must not stall requests) ---
    redis_client = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0,
    )
    application.state.redis = redis_client

    # --- Response cache ---
    cache = ResponseCache(embedder=embedder, index=index, redis=redis_client, settings=settings)
    cache.start_maintenance()
    application.state.cache = cache
    logger.info("cache.ready", semantic_enabled=cache.semantic_enabled, ttl_seconds=settings.cache_ttl_seconds)

    application.state.limiter = ConcurrencyLimiter(settings.max_concurrent_upstream)

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
    limiter = getattr(request.app.state, "limiter", None)
    return HealthResponse(redis=redis_status, upstream=limiter.stats() if limiter else None)


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
    cache: ResponseCache = request.app.state.cache
    router: ModelRouter = request.app.state.router
    settings: Settings = request.app.state.settings

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
            return await _stream_response(proxy_client, body, routing, getattr(request.app.state, "limiter", None))

        # --- Cache: fingerprint the ORIGINAL request, so the key reflects what the caller asked ---
        cache_mode = (get_header(request, "cache") or "").lower()  # "", "bypass" or "refresh"
        fp = build_fingerprint(
            body.model_dump(exclude_none=True), messages_dicts, routing.model_selected,
            max_temperature=settings.cache_max_temperature,
        )
        use_cache = fp.eligible and cache_mode != "bypass"
        cache_header = "MISS" if use_cache else "BYPASS"
        bypass_reason = None if use_cache else ("client_bypass" if fp.eligible else f"ineligible:{fp.reason}")

        if use_cache and cache_mode != "refresh":
            result = await cache.lookup(fp)
            if result.hit:
                return _cache_hit_response(request, routing, result.response, result.tier, result.similarity_score)

        # --- Single-flight: identical requests already running share one upstream call ---
        flight = cache.begin_flight(fp.exact_key) if use_cache else None
        if flight is not None and not flight.leader:
            try:
                return _cache_hit_response(request, routing, await flight.wait(), "coalesced", 1.0)
            except FlightAborted:
                flight = None  # the request we waited on was cancelled; do the work ourselves

        # --- Miss: compress (only now), call the provider, store ---
        no_compress = get_header(request, "no-compress") == "true"
        compression_ratio = 1.0
        limiter: ConcurrencyLimiter | None = getattr(request.app.state, "limiter", None)
        slot_taken = False
        try:
            if limiter is not None:
                limiter.acquire()  # 503 right away when saturated; the except below releases the flight
                slot_taken = True
            upstream_messages = messages_dicts
            if not no_compress:
                upstream_messages, compression_ratio = await compress_context(
                    messages_dicts, body.model, proxy_client, settings,
                )
            upstream_body = body.model_copy(update={"messages": [ChatMessage(**m) for m in upstream_messages]})
            response_data = await proxy_client.forward(upstream_body, model_override=routing.model_selected)
            if use_cache:
                await cache.store(fp, response_data)
        except BaseException as exc:
            if slot_taken:
                limiter.release()
                slot_taken = False
            if flight is not None:  # followers get the same error; a cancelled leader releases them to retry
                flight.finish(error=exc if isinstance(exc, Exception) else None)
            raise
        if slot_taken:
            limiter.release()
        if flight is not None:
            flight.finish(response=response_data)

        usage = response_data.get("usage") or {}
        request.state.cache_hit = False
        request.state.similarity_score = None
        request.state.prompt_tokens = usage.get("prompt_tokens", 0)
        request.state.completion_tokens = usage.get("completion_tokens", 0)
        request.state.compressed = compression_ratio != 1.0
        request.state.compression_ratio = compression_ratio

        headers = {
            "X-Cache": cache_header,
            "X-Model-Tier": routing.tier.value,
            "X-Model-Selected": routing.model_selected,
            "X-Compressed": str(compression_ratio != 1.0),
            "X-Compression-Ratio": str(compression_ratio),
        }
        if bypass_reason:
            headers["X-Cache-Reason"] = bypass_reason
        return JSONResponse(content=response_data, headers=headers)

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


def _cache_hit_response(request: Request, routing, response: dict, tier: str | None, similarity: float) -> JSONResponse:
    """Serve a cached (or coalesced) response and record it for telemetry.

    Token counts come from the stored response so that cache savings are computed from real
    numbers (the pre-2.0 code recorded zero tokens on a hit, which made savings always $0).
    """
    usage = response.get("usage") or {}
    request.state.cache_hit = True
    request.state.similarity_score = similarity
    request.state.prompt_tokens = usage.get("prompt_tokens", 0)
    request.state.completion_tokens = usage.get("completion_tokens", 0)
    request.state.compressed = False
    request.state.compression_ratio = 1.0
    return JSONResponse(
        content=response,
        headers={
            "X-Cache": "HIT",
            "X-Cache-Tier": tier or "exact",
            "X-Similarity": f"{similarity:.4f}",
            "X-Model-Tier": routing.tier.value,
            "X-Model-Selected": routing.model_selected,
        },
    )


async def _stream_response(
    proxy_client: ProxyClient, body: ChatCompletionRequest, routing, limiter: ConcurrencyLimiter | None = None,
) -> StreamingResponse:
    """Start the upstream stream, then hand back an SSE response.

    The first chunk is awaited *before* the response is created. Failures that happen while
    connecting (bad key, unknown model, rate limit, cooldown) therefore still produce a real
    HTTP error status. Once bytes are on the wire the status can't change, so a later failure
    is sent in-band as an ``error`` event, which the OpenAI SDKs raise as an exception.
    """
    if limiter is not None:
        limiter.acquire()  # held until the stream ends; released in events() or on a connect failure
    stream = proxy_client.forward_stream(body, model_override=routing.model_selected)
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        first = None
    except UpstreamError as exc:
        if limiter is not None:
            limiter.release()
        raise from_upstream(exc.status_code, exc.detail) from exc
    except BaseException:
        if limiter is not None:
            limiter.release()
        raise

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
            try:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
            finally:
                if limiter is not None:
                    limiter.release()

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


# ────────────────── Removed: adaptive threshold ──────────────────────────
# The adaptive controller raised or lowered the similarity threshold from the observed hit
# rate, which rewards admitting wrong answers (docs/TECHNICAL_AUDIT.md, C-3). It is gone; these
# routes answer 410 for one release so old callers get an explanation instead of a 404.

_REMOVED_MESSAGE = (
    "The adaptive similarity threshold was removed in NightJar 2.0. Set SIMILARITY_THRESHOLD "
    "(used only when SEMANTIC_CACHE=true) or calibrate offline with benchmarks/cache_eval.py."
)


@app.api_route("/v1/threshold", methods=["GET"], dependencies=[Depends(require_api_key)])
@app.api_route("/v1/threshold/history", methods=["GET"], dependencies=[Depends(require_api_key)])
@app.api_route("/v1/threshold/evaluate", methods=["POST"], dependencies=[Depends(require_api_key)])
async def threshold_removed():
    raise NightJarError(410, _REMOVED_MESSAGE, type="invalid_request_error", code="endpoint_removed")


# ─────────────────── Cache Invalidation Endpoints ────────────────────────


@app.get("/v1/cache/stats", dependencies=[Depends(require_api_key)])
async def cache_stats(request: Request):
    """Return cache statistics."""
    settings: Settings = request.app.state.settings
    cache: ResponseCache = request.app.state.cache
    stats = await cache.stats()
    stats["similarity_threshold"] = settings.similarity_threshold
    return stats


@app.delete("/v1/cache", dependencies=[Depends(require_api_key)])
async def flush_cache(request: Request):
    """Flush the entire semantic cache."""
    cache: ResponseCache = request.app.state.cache
    result = await cache.flush()
    return {"status": "ok", **result}


@app.delete("/v1/cache/{key}", dependencies=[Depends(require_api_key)])
async def invalidate_cache_key(key: str, request: Request):
    """Invalidate a specific cache entry by key."""
    cache: ResponseCache = request.app.state.cache
    removed = await cache.invalidate(key)
    return {"status": "ok", "key": key, "removed": removed}
