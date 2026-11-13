"""Error types and OpenAI-shaped error bodies.

Every error NightJar returns uses the shape OpenAI SDKs parse::

    {"error": {"message": "...", "type": "...", "param": null, "code": "..."}}

(See docs/research/SOURCE_REFERENCES.md #11: the exact field set is the one the
SDKs and other gateways use; it was not confirmed on an official OpenAI page.)

Upstream provider failures are translated, not forwarded: a provider's 401
means *NightJar's* provider key is bad, which must not reach the caller as a
401 (callers would think their own NightJar key was rejected).
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger()

MAX_UPSTREAM_MESSAGE_CHARS = 500

# Things that look like credentials. Provider error text sometimes echoes them.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(api[_-]?key[\"'=:\s]+)[A-Za-z0-9._\-]{8,}"),
]


def redact_secrets(text: str) -> str:
    """Mask credential-looking substrings in provider-supplied text."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    return text


class NightJarError(Exception):
    """An error with an HTTP status and an OpenAI-style body."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type: str = "invalid_request_error",  # noqa: A002 - mirrors the API field name
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type
        self.code = code
        self.param = param
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return error_body(self.message, self.type, self.code, self.param)


def error_body(message: str, type: str, code: str | None = None, param: str | None = None) -> dict[str, Any]:  # noqa: A002
    return {"error": {"message": message, "type": type, "param": param, "code": code}}


# ── Constructors for the cases the gateway produces itself ──────────────────


def invalid_request(message: str, *, param: str | None = None, code: str | None = None) -> NightJarError:
    return NightJarError(400, message, type="invalid_request_error", param=param, code=code)


DEFAULT_AUTH_MESSAGE = "Missing or invalid API key. Provide 'Authorization: Bearer <key>'."


def authentication_error(message: str = DEFAULT_AUTH_MESSAGE) -> NightJarError:
    return NightJarError(
        401, message, type="authentication_error", code="invalid_api_key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def payload_too_large(limit_bytes: int) -> NightJarError:
    return NightJarError(
        413, f"Request body exceeds the {limit_bytes} byte limit.",
        type="invalid_request_error", code="request_too_large",
    )


def overloaded(limit: int) -> NightJarError:
    return NightJarError(
        503, f"The gateway is at its limit of {limit} concurrent upstream requests. Retry shortly.",
        type="upstream_error", code="gateway_overloaded", headers={"Retry-After": "1"},
    )


def context_too_large(chars: int, limit: int) -> NightJarError:
    return NightJarError(
        400, f"Messages contain {chars} characters of text; the limit is {limit}.",
        type="invalid_request_error", code="context_length_exceeded", param="messages",
    )


def from_upstream(status_code: int, detail: str) -> NightJarError:
    """Translate a provider failure into an error that is correct for *our* caller."""
    message = redact_secrets(detail).strip()
    if len(message) > MAX_UPSTREAM_MESSAGE_CHARS:
        message = message[:MAX_UPSTREAM_MESSAGE_CHARS] + "…"

    if status_code in (401, 403):
        return NightJarError(
            502, "The upstream provider rejected NightJar's credentials. This is a gateway configuration "
            "problem, not an issue with your request or API key.",
            type="upstream_error", code="upstream_auth_error",
        )
    if status_code == 429:
        return NightJarError(
            429, f"Upstream rate limit: {message}", type="rate_limit_error", code="upstream_rate_limit",
        )
    if status_code == 404:
        return NightJarError(404, f"Model or resource not found upstream: {message}",
                             type="invalid_request_error", code="model_not_found", param="model")
    if status_code in (400, 413, 422):
        return NightJarError(400, f"Upstream rejected the request: {message}", type="invalid_request_error",
                             code="upstream_invalid_request")
    # Server-side failures get fixed text. The provider's own wording adds nothing a caller can act
    # on and can leak internals; the detail is logged with the request id instead.
    if status_code in (408, 504):
        return NightJarError(504, "The upstream provider timed out.", type="upstream_error", code="upstream_timeout")
    if status_code == 503:
        return NightJarError(503, "The upstream provider is temporarily unavailable.", type="upstream_error",
                             code="upstream_unavailable", headers={"Retry-After": "5"})
    return NightJarError(502, "The upstream provider returned an error.", type="upstream_error", code="upstream_error")


# ── Handlers ────────────────────────────────────────────────────────────────


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _respond(request: Request, status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None):
    return JSONResponse(status_code=status_code, content=body, headers=headers)


async def _nightjar_error_handler(request: Request, exc: NightJarError) -> JSONResponse:
    request.state.error_code = exc.code
    return _respond(request, exc.status_code, exc.body(), exc.headers)


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = [str(p) for p in first.get("loc", ()) if p != "body"]
    param = ".".join(loc) or None
    msg = first.get("msg", "Invalid request")
    if first.get("type") == "json_invalid":
        message, code = "Request body is not valid JSON.", "invalid_json"
        param = None
    else:
        message = f"{param}: {msg}" if param else msg
        code = "invalid_request"
    if len(errors) > 1:
        message += f" (and {len(errors) - 1} more problem{'s' if len(errors) > 2 else ''})"
    return _respond(request, 400, error_body(message, "invalid_request_error", code, param))


_HTTP_TYPES = {
    401: ("authentication_error", "invalid_api_key"),
    403: ("permission_error", "forbidden"),
    404: ("invalid_request_error", "not_found"),
    405: ("invalid_request_error", "method_not_allowed"),
    429: ("rate_limit_error", "rate_limit_exceeded"),
}


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    default = ("invalid_request_error" if exc.status_code < 500 else "server_error", None)
    etype, code = _HTTP_TYPES.get(exc.status_code, default)
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return _respond(request, exc.status_code, error_body(message, etype, code), getattr(exc, "headers", None))


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = _request_id(request)
    logger.error("unhandled_exception", request_id=rid, path=request.url.path, exc_info=exc)
    message = "Internal server error."
    if rid:
        message += f" Include request id {rid} when reporting this."
    return _respond(request, 500, error_body(message, "server_error", "internal_error"))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NightJarError, _nightjar_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_handler)


def sse_error_event(err: NightJarError) -> str:
    """An in-band error for a stream that has already started (HTTP status can't change)."""
    import json

    return f"data: {json.dumps(err.body())}\n\n"
