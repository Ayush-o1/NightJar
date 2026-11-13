"""Canonical request fingerprints and cache eligibility.

A cached answer may be served for a new request only if the new request would
have produced an answer from the same distribution. That depends on far more
than the last user message: the model, sampling parameters, tool definitions,
response format, the system prompt and the conversation so far.

The fingerprint has two parts:

``partition``
    hash of (model, generation parameters, every message *before* the final user
    message). Two requests in the same partition differ only in their last user
    message. The semantic tier searches only inside a partition.

``exact_key``
    hash of (partition, final message). Identical requests get identical keys,
    which is what the exact tier uses.

Unknown request fields are included in the hash by default. Ignoring an
unrecognised field could merge two requests that should differ, and a wrong
hit is worse than a miss (docs/research/TECHNICAL_FINDINGS.md, F2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.tokens import content_to_text

# Request fields that do not change what the model generates.
_IGNORED_FIELDS = frozenset({"model", "messages", "stream", "stream_options", "user", "metadata", "store"})

# Provider default when the caller does not set a temperature. Assumed, not verified per provider.
DEFAULT_TEMPERATURE = 1.0

CACHE_VERSION = "v2"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    eligible: bool
    reason: str  # why it is ineligible, or "ok"
    model: str = ""
    partition: str = ""
    exact_key: str = ""
    # Text of the final user message, for the semantic tier. None when that tier can't be used
    # (final message isn't plain user text, or the request contains media).
    semantic_text: str | None = None


def _ineligible(reason: str) -> Fingerprint:
    return Fingerprint(eligible=False, reason=reason)


def build_fingerprint(
    request_fields: dict[str, Any],
    messages: list[dict],
    model: str,
    *,
    max_temperature: float = DEFAULT_TEMPERATURE,
) -> Fingerprint:
    """Fingerprint a request. ``model`` is the model that will actually serve it.

    ``request_fields`` is the request body as a dict (including ``messages``; it is ignored
    there). ``messages`` are the original, uncompressed messages.
    """
    if not messages:
        return _ineligible("no_messages")

    params = {k: v for k, v in request_fields.items() if k not in _IGNORED_FIELDS and v is not None}

    if params.get("n", 1) != 1:
        return _ineligible("n_greater_than_1")
    # Tool use has side effects and non-text outputs; not cached in either tier.
    if any(k in params for k in ("tools", "functions", "tool_choice", "function_call")):
        return _ineligible("tools")
    if params.get("temperature", DEFAULT_TEMPERATURE) > max_temperature:
        return _ineligible("temperature_above_cutoff")

    last = messages[-1]
    context = messages[:-1]
    partition = _digest(CACHE_VERSION, model, _canonical(params), _canonical(context))
    exact_key = _digest(partition, _canonical(last))

    semantic_text: str | None = None
    if last.get("role") == "user" and not last.get("tool_calls"):
        content = last.get("content")
        plain = isinstance(content, str) or (
            isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in content)
        )
        has_media = any(
            isinstance(m.get("content"), list)
            and any(isinstance(p, dict) and p.get("type") != "text" for p in m["content"])
            for m in messages
        )
        if plain and not has_media:
            semantic_text = content_to_text(content)

    return Fingerprint(
        eligible=True, reason="ok", model=model, partition=partition, exact_key=exact_key,
        semantic_text=semantic_text or None,
    )


def response_is_cacheable(response: dict) -> tuple[bool, str]:
    """Only complete, plain-text, single-choice answers are stored."""
    choices = response.get("choices") or []
    if len(choices) != 1:
        return False, "not_single_choice"
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        return False, f"finish_reason_{choice.get('finish_reason')}"
    message = choice.get("message") or {}
    if message.get("tool_calls") or message.get("function_call"):
        return False, "tool_calls"
    if not content_to_text(message.get("content")).strip():
        return False, "empty_content"
    return True, "ok"
