"""Optional bearer-token authentication for the gateway itself.

Distinct from the upstream provider keys in app/config.py (OPENAI_API_KEY,
etc.) — those authenticate NightJar *to* the LLM providers. This module
authenticates callers *to* NightJar, so the gateway can be safely exposed
beyond localhost.

Disabled by default (NIGHTJAR_API_KEYS unset) to keep local single-user
development frictionless. Set NIGHTJAR_API_KEYS to one or more
comma-separated tokens to require `Authorization: Bearer <token>` on every
protected request. See docs/ARCHITECTURE.md for the threat model.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from app.config import Settings


def _matches_any(token: str, valid_keys: list[str]) -> bool:
    """Constant-time membership check against the configured key list."""
    return any(hmac.compare_digest(token, key) for key in valid_keys)


async def require_api_key(request: Request) -> None:
    """FastAPI dependency: enforce bearer-token auth when configured.

    No-op when NIGHTJAR_API_KEYS is unset (auth_enabled == False), so
    existing local-dev usage is unaffected unless the operator opts in.
    """
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not _matches_any(token, settings.api_keys):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key. Provide 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
