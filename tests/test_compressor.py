"""Tests for app/compressor.py — context compression logic.

Covers:
  - Token counting accuracy
  - should_compress() threshold gates
  - compress_context() happy path, error fallback, system message preservation
  - compress_context_with_metadata() metadata fields
  - Header bypass (no-compress) integration with the pipeline
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.compressor import (
    _SUMMARY_MARKER,
    compress_context,
    compress_context_with_metadata,
    count_tokens,
    should_compress,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────


def make_messages(n: int, include_system: bool = False) -> list[dict]:
    """Build n user/assistant pairs, optionally prefixed by a system message."""
    msgs: list[dict] = []
    if include_system:
        msgs.append({"role": "system", "content": "You are a helpful assistant."})
    for i in range(n):
        msgs.append({"role": "user", "content": f"Question number {i} about something interesting and detailed"})
        msgs.append({"role": "assistant", "content": f"Answer number {i} with a detailed and thorough response"})
    return msgs


def make_settings(
    threshold: int = 500,
    keep_recent: int = 4,
    min_turns: int = 6,
    summary_model: str = "gpt-3.5-turbo",
) -> MagicMock:
    s = MagicMock()
    s.compress_threshold = threshold
    s.compress_keep_recent = keep_recent
    s.compress_min_turns = min_turns
    s.compress_summary_model = summary_model
    return s


def make_mock_client(summary_text: str = "Summary of earlier conversation.") -> MagicMock:
    client = MagicMock()
    client.simple_completion = AsyncMock(
        return_value={"choices": [{"message": {"content": summary_text}}]}
    )
    return client


# ═══════════════════════════════════════════════════════════════════════════
# 1. Token counting
# ═══════════════════════════════════════════════════════════════════════════


def test_token_counting_returns_positive():
    msgs = [{"role": "user", "content": "Hello world"}]
    assert count_tokens(msgs) > 0


def test_token_counting_empty_content():
    """Messages with empty or None content must not raise."""
    msgs = [{"role": "user", "content": ""}, {"role": "assistant", "content": None}]
    result = count_tokens(msgs)
    assert result >= 0


def test_token_counting_unknown_model_fallback():
    """Unknown model names must fall back to cl100k_base without raising."""
    msgs = [{"role": "user", "content": "Test message"}]
    count = count_tokens(msgs, model="some-unknown-future-model-xyz")
    assert count > 0


def test_token_count_scales_with_content_length():
    """More content → more tokens."""
    short = [{"role": "user", "content": "Hi"}]
    long = [{"role": "user", "content": "Hi " * 500}]
    assert count_tokens(long) > count_tokens(short)


# ═══════════════════════════════════════════════════════════════════════════
# 2. should_compress() threshold gates
# ═══════════════════════════════════════════════════════════════════════════


def test_should_compress_false_below_token_threshold():
    """Short messages that are under the token threshold must NOT trigger compression."""
    msgs = make_messages(5)  # 10 messages, but few tokens
    settings = make_settings(threshold=999999)  # impossibly high threshold
    assert should_compress(msgs, settings=settings) is False


def test_should_compress_false_below_min_turns():
    """Conversations under min_turns must NOT be compressed even if token-heavy."""
    msgs = make_messages(2)  # 4 non-system messages, below min_turns=6
    settings = make_settings(threshold=1)  # very low threshold
    assert should_compress(msgs, settings=settings) is False


def test_should_compress_true_when_both_gates_pass():
    """should_compress must return True when both token count AND turn count exceed thresholds."""
    msgs = make_messages(10)  # 20 messages, well above min_turns=6
    settings = make_settings(threshold=10)  # tiny threshold → many tokens
    assert should_compress(msgs, settings=settings) is True


def test_should_compress_ignores_system_messages_in_turn_count():
    """System messages must not count toward the turn minimum."""
    # 3 user/assistant pairs = 6 non-system turns, but with system included
    msgs = [{"role": "system", "content": "Instructions."}] + make_messages(3)
    settings = make_settings(threshold=1, min_turns=7)  # need 7 non-system turns
    assert should_compress(msgs, settings=settings) is False


# ═══════════════════════════════════════════════════════════════════════════
# 3. compress_context() — original 5 tests (kept as-is for regression)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_compression_below_min_turns():
    messages = make_messages(2)  # 4 messages, below min_turns=6
    mock_client = MagicMock()
    settings = make_settings()

    result, ratio = await compress_context(messages, "gpt-3.5-turbo", mock_client, settings)

    assert result == messages
    assert ratio == 1.0


@pytest.mark.asyncio
async def test_compression_reduces_message_count():
    messages = make_messages(10)
    mock_client = make_mock_client()
    settings = make_settings(threshold=100)

    result, ratio = await compress_context(messages, "gpt-3.5-turbo", mock_client, settings)

    assert len(result) < len(messages)
    assert ratio < 1.0


@pytest.mark.asyncio
async def test_fallback_on_error_returns_original():
    messages = make_messages(10)
    mock_client = MagicMock()
    mock_client.simple_completion = AsyncMock(side_effect=Exception("API down"))
    settings = make_settings(threshold=100)

    result, ratio = await compress_context(messages, "gpt-3.5-turbo", mock_client, settings)

    assert result == messages
    assert ratio == 1.0


@pytest.mark.asyncio
async def test_system_messages_preserved():
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    messages += make_messages(10)
    mock_client = make_mock_client()
    settings = make_settings(threshold=100)

    result, ratio = await compress_context(messages, "gpt-3.5-turbo", mock_client, settings)

    assert result[0]["role"] == "system"
    assert result[0]["content"] == "You are a helpful assistant."


# ═══════════════════════════════════════════════════════════════════════════
# 4. compress_context_with_metadata()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_metadata_no_compression_has_correct_defaults():
    """When compression doesn't trigger, metadata must report no compression."""
    messages = make_messages(2)
    mock_client = MagicMock()
    settings = make_settings()

    _, meta = await compress_context_with_metadata(messages, "gpt-3.5-turbo", mock_client, settings)

    assert meta["compressed"] is False
    assert meta["compression_ratio"] == 1.0
    assert meta["turns_summarized"] == 0
    assert meta["savings_pct"] == 0.0
    assert meta["original_tokens"] == meta["compressed_tokens"]


@pytest.mark.asyncio
async def test_metadata_compression_ran():
    """When compression fires, metadata must reflect accurate before/after token counts."""
    messages = make_messages(10)
    mock_client = make_mock_client("Short summary.")
    settings = make_settings(threshold=100)

    compressed, meta = await compress_context_with_metadata(
        messages, "gpt-3.5-turbo", mock_client, settings
    )

    assert meta["compressed"] is True
    assert meta["turns_summarized"] > 0
    assert meta["compressed_tokens"] < meta["original_tokens"]
    assert 0 < meta["compression_ratio"] < 1.0
    assert meta["savings_pct"] > 0
    assert meta["summary_model"] == "gpt-3.5-turbo"


@pytest.mark.asyncio
async def test_metadata_summary_marker_in_output():
    """The summary message must contain the SUMMARY_MARKER sentinel string."""
    messages = make_messages(10)
    mock_client = make_mock_client("Key points: A, B, C.")
    settings = make_settings(threshold=100)

    compressed, _ = await compress_context_with_metadata(
        messages, "gpt-3.5-turbo", mock_client, settings
    )

    summary_msgs = [m for m in compressed if _SUMMARY_MARKER in (m.get("content") or "")]
    assert len(summary_msgs) == 1


@pytest.mark.asyncio
async def test_recent_turns_kept_verbatim():
    """The most-recent `keep_recent` non-system turns must appear unchanged at end."""
    keep_recent = 4
    n_pairs = 8
    messages = make_messages(n_pairs)
    mock_client = make_mock_client()
    settings = make_settings(threshold=10, keep_recent=keep_recent)

    compressed, meta = await compress_context_with_metadata(
        messages, "gpt-3.5-turbo", mock_client, settings
    )

    # The last keep_recent messages of the input should match the tail of compressed
    expected_tail = messages[-keep_recent:]
    actual_tail = compressed[-keep_recent:]
    assert actual_tail == expected_tail


@pytest.mark.asyncio
async def test_metadata_fallback_on_error():
    """On summarisation failure, metadata must still be returned with compressed=False."""
    messages = make_messages(10)
    mock_client = MagicMock()
    mock_client.simple_completion = AsyncMock(side_effect=Exception("Timeout"))
    settings = make_settings(threshold=10)

    result, meta = await compress_context_with_metadata(
        messages, "gpt-3.5-turbo", mock_client, settings
    )

    assert result == messages
    assert meta["compressed"] is False
    assert meta["compression_ratio"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# 5. Header bypass integration test
# ═══════════════════════════════════════════════════════════════════════════


def test_no_compress_header_bypasses_compression(test_client, mock_proxy_client, chat_completion_fixture):
    """When X-NightJar-No-Compress: true is sent, compressor must not be called."""
    mock_proxy_client.forward.return_value = chat_completion_fixture

    with patch("app.main.compress_context") as mock_compress:
        resp = test_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"X-NightJar-No-Compress": "true"},
        )

    assert resp.status_code == 200
    mock_compress.assert_not_called()
    assert resp.headers.get("X-Compressed") == "False"
