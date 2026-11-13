"""Per-request telemetry — writes one row per proxied /v1/chat/* call.

Pure ASGI so latency covers the whole response, including a stream that is
still sending tokens. (The previous BaseHTTPMiddleware version recorded the time
the response *headers* were ready, so a 30-second stream was logged as ~100 ms.)

The row is written when the final body chunk is sent, or when the connection
ends early (client disconnect, upstream failure mid-stream), whichever comes
first, exactly once.

This module is replaced by the unified request recorder in the telemetry phase.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import telemetry
from app.asgi import new_request_id
from app.costs import estimate_cost


class TelemetryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/v1/chat"):
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        start = time.monotonic()
        recorded = False

        async def record() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            # The handler sets model_requested before anything else can fail, so its absence
            # means the request never reached the proxy pipeline (a 4xx from validation or
            # auth). Recording those would inflate totals and skew the cache-hit rate.
            if "model_requested" not in state:
                return
            latency_ms = (time.monotonic() - start) * 1000
            prompt_tokens = state.get("prompt_tokens", 0)
            completion_tokens = state.get("completion_tokens", 0)
            record_dict = {
                "request_id": state.get("request_id") or new_request_id(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model_requested": state.get("model_requested"),
                "model_used": state.get("model_used"),
                "tier": state.get("tier"),
                "routing_reason": state.get("routing_reason"),
                "cache_hit": state.get("cache_hit", False),
                "similarity_score": state.get("similarity_score"),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                # A cache hit made no provider call, so it cost nothing. Its token counts are kept so
                # savings can be computed from them.
                "estimated_cost_usd": 0.0 if state.get("cache_hit") else estimate_cost(
                    state.get("model_used") or "", prompt_tokens, completion_tokens),
                "latency_ms": round(latency_ms, 2),
                "compressed": state.get("compressed", False),
                "compression_ratio": state.get("compression_ratio", 1.0),
            }
            # SQLite is synchronous; keep it off the event loop.
            await asyncio.to_thread(telemetry.write_record, record_dict)

        async def send_wrapper(message: Message) -> None:
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                await record()

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            await record()
