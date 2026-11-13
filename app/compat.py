"""Compatibility shims for names that changed in the ContextForge → NightJar rename.

Kept in one module so the whole deprecation surface is visible and can be
deleted together. Nothing here is used by new code paths except `get_header`.
"""

from __future__ import annotations

import structlog
from starlette.requests import Request

logger = structlog.get_logger()

_LEGACY_PREFIX = "x-contextforge-"
_PREFIX = "x-nightjar-"


def get_header(request: Request, suffix: str) -> str | None:
    """Read ``X-NightJar-<suffix>``, falling back to the pre-rename ``X-ContextForge-<suffix>``.

    The new name wins when both are present.
    """
    value = request.headers.get(f"{_PREFIX}{suffix}")
    if value is not None:
        return value
    legacy = request.headers.get(f"{_LEGACY_PREFIX}{suffix}")
    if legacy is not None:
        logger.warning(
            "deprecated.header",
            header=f"X-ContextForge-{suffix}",
            use_instead=f"X-NightJar-{suffix}",
        )
    return legacy
