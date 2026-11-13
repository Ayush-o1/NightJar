"""Context compressor — summarizes older turns to reduce token usage.

Public API
----------
count_tokens(messages, model)           → int
should_compress(messages, model, settings) → bool
compress_context(messages, model, proxy_client, settings)
    → tuple[list[dict], float]          # (compressed_messages, ratio)
compress_context_with_metadata(...)
    → tuple[list[dict], dict]           # (compressed_messages, metadata_dict)
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.tokens import content_to_text, count_message_tokens

logger = logging.getLogger(__name__)

# ─── Summarisation prompt ─────────────────────────────────────────────────

_SUMMARY_PROMPT = (
    "Summarize the following conversation turns into a single concise paragraph. "
    "Preserve all key facts, decisions, code snippets, and context that will be "
    "needed to continue the conversation coherently:\n\n{turns}"
)

_SUMMARY_MARKER = "[SUMMARY OF EARLIER CONVERSATION]"


# ─── Token counting ───────────────────────────────────────────────────────


def count_tokens(messages: list[dict], model: str = "gpt-3.5-turbo") -> int:
    """Total tokens across messages (bounded per message, includes tool calls; see app/tokens.py)."""
    return count_message_tokens(messages, model)


# ─── Compression trigger ──────────────────────────────────────────────────


def should_compress(
    messages: list[dict],
    model: str = "gpt-3.5-turbo",
    settings: Settings | None = None,
) -> bool:
    """Return True if the conversation exceeds compression thresholds.

    Both conditions must be met:
    - total token count > compress_threshold
    - non-system message count > compress_min_turns
    """
    if settings is None:
        from app.config import get_settings
        settings = get_settings()

    non_system = [m for m in messages if m.get("role") != "system"]
    total_tokens = count_tokens(messages, model)

    if total_tokens <= settings.compress_threshold:
        return False
    if len(non_system) <= settings.compress_min_turns:
        return False
    return True


# ─── Core compression helpers ─────────────────────────────────────────────


def _split_messages(
    messages: list[dict], keep_recent: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split messages into (system_msgs, turns_to_summarize, recent_turns).

    System messages are always kept verbatim.  Of the remaining turns,
    the most recent `keep_recent` are kept verbatim and the rest are
    candidates for summarisation.
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    split_idx = max(1, len(non_system) - keep_recent)
    turns_to_summarize = non_system[:split_idx]
    recent_turns = non_system[split_idx:]
    return system_msgs, turns_to_summarize, recent_turns


def _format_turns_for_summary(turns: list[dict]) -> str:
    """Render a list of message dicts as a readable turn-by-turn transcript."""
    return "\n".join(
        f"{m['role'].upper()}: {content_to_text(m.get('content'))}" for m in turns
    )


# ─── Public API ───────────────────────────────────────────────────────────


async def compress_context(
    messages: list[dict],
    model: str,
    proxy_client,
    settings: Settings,
) -> tuple[list[dict], float]:
    """Summarise older turns when total tokens exceed threshold.

    Returns
    -------
    (messages, compression_ratio)
        compression_ratio == 1.0 means no compression was performed.
        Falls back to the original messages on any error.
    """
    compressed, meta = await compress_context_with_metadata(
        messages, model, proxy_client, settings
    )
    return compressed, meta["compression_ratio"]


async def compress_context_with_metadata(
    messages: list[dict],
    model: str,
    proxy_client,
    settings: Settings,
) -> tuple[list[dict], dict]:
    """Summarise older turns and return rich compression metadata.

    Returns
    -------
    (compressed_messages, metadata)

    metadata fields
    ---------------
    compressed          : bool   — True if compression actually ran
    original_tokens     : int    — token count before compression
    compressed_tokens   : int    — token count after compression
    compression_ratio   : float  — compressed / original (lower = more savings)
    turns_summarized    : int    — number of turns that were replaced
    summary_model       : str    — model used for summarisation
    savings_pct         : float  — percentage token reduction (0–100)
    """
    original_tokens = count_tokens(messages, model)
    _noop_meta = {
        "compressed": False,
        "original_tokens": original_tokens,
        "compressed_tokens": original_tokens,
        "compression_ratio": 1.0,
        "turns_summarized": 0,
        "summary_model": settings.compress_summary_model,
        "savings_pct": 0.0,
    }

    # Gate 1: token threshold
    if original_tokens <= settings.compress_threshold:
        return messages, _noop_meta

    # Gate 2: minimum conversation length
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) <= settings.compress_min_turns:
        return messages, _noop_meta

    system_msgs, turns_to_summarize, recent_turns = _split_messages(
        messages, settings.compress_keep_recent
    )

    if not turns_to_summarize:
        return messages, _noop_meta

    try:
        transcript = _format_turns_for_summary(turns_to_summarize)
        summary_prompt = _SUMMARY_PROMPT.format(turns=transcript)

        summary_response = await proxy_client.simple_completion(
            messages=[{"role": "user", "content": summary_prompt}],
            model=settings.compress_summary_model,
        )
        summary_text = summary_response["choices"][0]["message"]["content"]

        summary_message = {
            "role": "user",
            "content": f"{_SUMMARY_MARKER}: {summary_text}",
        }
        compressed = system_msgs + [summary_message] + recent_turns

        compressed_tokens = count_tokens(compressed, model)
        ratio = compressed_tokens / original_tokens
        savings_pct = round((1 - ratio) * 100, 1)

        logger.info(
            "compressor.context_compressed",
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            ratio=f"{ratio:.2f}",
            savings_pct=f"{savings_pct}%",
            turns_summarized=len(turns_to_summarize),
        )

        meta = {
            "compressed": True,
            "original_tokens": original_tokens,
            "compressed_tokens": compressed_tokens,
            "compression_ratio": round(ratio, 4),
            "turns_summarized": len(turns_to_summarize),
            "summary_model": settings.compress_summary_model,
            "savings_pct": savings_pct,
        }
        return compressed, meta

    except Exception as exc:  # noqa: BLE001
        logger.warning("compressor.failed_graceful_fallback reason=%s", str(exc))
        return messages, _noop_meta
