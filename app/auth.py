"""Authentication and authorization for calls *to* NightJar.

Distinct from the provider keys in app/config.py (OPENAI_API_KEY, ...), which authenticate NightJar
*to* the providers.

Modes (``NIGHTJAR_AUTH``):
  auto      (default) auth is enforced as soon as any key exists (env or key store); with no keys the
            gateway is open, which suits ``uvicorn`` on localhost
  required  always enforced; with no keys every protected request is 401 (the Docker image sets this)
  off       never enforced (explicit opt-out)

Callers present ``Authorization: Bearer <key>``. A key resolves to a Principal (name, scopes, model
allow-list, rate limit) that routes can authorize against and telemetry can attribute to.
"""

from __future__ import annotations

import hmac

from fastapi import Request

from app.config import Settings
from app.errors import NightJarError, authentication_error
from app.keys import ANONYMOUS, SCOPES, KeyStore, Principal


def _matches(token: str, keys: list[str]) -> int | None:
    """Index of the env key equal to ``token`` (constant-time per comparison), else None."""
    found = None
    for i, key in enumerate(keys):
        if hmac.compare_digest(token, key):
            found = i
    return found


def auth_enforced(settings: Settings, store: KeyStore | None) -> bool:
    mode = settings.nightjar_auth
    if mode == "off":
        return False
    if mode == "required":
        return True
    return bool(settings.api_keys) or bool(store and store.has_active_keys())


async def authenticate(request: Request) -> Principal:
    """Resolve the caller. Raises 401 when auth is enforced and the key is missing or invalid."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    settings: Settings = request.app.state.settings
    store: KeyStore | None = getattr(request.app.state, "key_store", None)

    if not auth_enforced(settings, store):
        request.state.principal = ANONYMOUS
        return ANONYMOUS

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise authentication_error()

    principal: Principal | None = None
    idx = _matches(token, settings.api_keys)
    if idx is not None:
        principal = Principal(name=f"env-key-{idx + 1}", scopes=SCOPES)
    elif store is not None:
        principal = store.authenticate(token)
    if principal is None:
        raise authentication_error()
    request.state.principal = principal
    return principal


def require_scope(scope: str):
    """FastAPI dependency factory: authenticate, then require ``scope``."""

    async def dependency(request: Request) -> Principal:
        principal = await authenticate(request)
        if not principal.has(scope):
            raise NightJarError(
                403, f"This API key does not have the {scope!r} scope.",
                type="permission_error", code="insufficient_scope",
            )
        return principal

    return dependency


# Kept for callers/tests written before scopes existed: "any valid key".
async def require_api_key(request: Request) -> None:
    await authenticate(request)
