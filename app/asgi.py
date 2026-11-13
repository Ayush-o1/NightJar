"""Pure-ASGI middleware: request IDs, body size limit, unhandled-error safety net.

Written as raw ASGI rather than ``BaseHTTPMiddleware`` because it has to act on
the response *stream* (headers now, body later) and on the request *body as it
arrives*, which ``BaseHTTPMiddleware`` hides (see docs/research/SOURCE_REFERENCES.md #12
for how well-sourced that recommendation is).

What each piece guards against:

* Request ID   - a caller can quote it, and it keys the telemetry row.
* Body limit   - without it one request can exhaust memory/CPU (docs/BASELINE.md).
                 Checked on ``Content-Length`` first (cheap, rejects before reading),
                 then counted while the body streams for chunked uploads.
* Safety net   - any exception escaping the app becomes a JSON 500 that still
                 carries the request ID, instead of a bare text body without it.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import error_body, payload_too_large

logger = structlog.get_logger()

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:24]


class _BodyTooLarge(Exception):
    pass


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        inbound = headers.get("x-request-id", "")
        request_id = inbound if _SAFE_REQUEST_ID.match(inbound) else new_request_id()

        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["started_at"] = time.monotonic()

        response_started = False
        too_large = False

        async def raw_send(message: Message) -> None:
            """Send with the request-id header attached to the response start."""
            if message["type"] == "http.response.start":
                out = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                out.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": out}
            await send(message)

        async def app_send(message: Message) -> None:
            nonlocal response_started
            if too_large:
                return  # the framework's reaction to an aborted body is replaced by our 413
            if message["type"] == "http.response.start":
                response_started = True
            await raw_send(message)

        # 1. Cheap rejection from the declared length, before reading anything.
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_body_bytes:
                    await self._reject(scope, receive, raw_send)
                    return
            except ValueError:
                pass  # malformed header; the server layer will deal with it

        # 2. Count bytes as they arrive (chunked bodies have no Content-Length).
        received = 0

        async def counting_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, counting_receive, app_send)
        except _BodyTooLarge:
            pass
        except Exception as exc:  # noqa: BLE001 - last line of defence, logged below
            if not too_large:
                state["error_code"] = "internal_error"
                logger.error("unhandled_exception", request_id=request_id, path=scope.get("path"), exc_info=exc)
                if response_started:
                    raise
                body = error_body(
                    f"Internal server error. Include request id {request_id} when reporting this.",
                    "server_error", "internal_error",
                )
                await JSONResponse(body, status_code=500)(scope, receive, raw_send)
                return

        if too_large and not response_started:
            await self._reject(scope, receive, raw_send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        err = payload_too_large(self.max_body_bytes)
        await JSONResponse(err.body(), status_code=413, headers={"Connection": "close"})(scope, receive, send)
