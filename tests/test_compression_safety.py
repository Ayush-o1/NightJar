"""Compression must keep tool conversations valid, reuse summaries, and fall back safely."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.compressor import (
    _split_messages,
    clear_summary_memo,
    compress_context_with_metadata,
)
from app.config import Settings

SETTINGS = Settings(
    compress_threshold=50, compress_keep_recent=2, compress_min_turns=3, compress_summary_model="sum-model",
)


def long_text(n=40):
    return "lorem ipsum dolor sit amet " * n


def call(i):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}


def reply(i):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"}


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_summary_memo()


def proxy(summary="short summary"):
    p = AsyncMock()
    p.simple_completion.return_value = {"choices": [{"message": {"content": summary}}]}
    return p


class TestToolBoundary:
    def test_tool_reply_is_never_orphaned_from_its_call(self):
        msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"}, call(1), reply(1), {"role": "assistant", "content": "done"}]
        # keep_recent=2 would start the kept window at reply(1) and orphan it from call(1)
        _, summarised, recent = _split_messages(msgs, keep_recent=2)
        assert recent[0]["role"] != "tool"
        kept_ids = {tc["id"] for m in recent for tc in m.get("tool_calls", [])}
        for m in recent:
            if m["role"] == "tool":
                assert m["tool_call_id"] in kept_ids
        assert summarised + recent == msgs

    def test_parallel_tool_calls_stay_together(self):
        two_calls = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}
        msgs = [{"role": "user", "content": "q"}, two_calls,
                {"role": "tool", "tool_call_id": "a", "content": "1"},
                {"role": "tool", "tool_call_id": "b", "content": "2"}, {"role": "assistant", "content": "ok"}]
        _, _, recent = _split_messages(msgs, keep_recent=2)
        assert recent[0] is two_calls

    def test_if_everything_is_one_tool_group_nothing_is_summarised(self):
        msgs = [call(1), reply(1)]
        _, summarised, recent = _split_messages(msgs, keep_recent=1)
        assert summarised == [] and recent == msgs

    def test_plain_conversations_split_as_before(self):
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
        _, summarised, recent = _split_messages(msgs, keep_recent=2)
        assert len(summarised) == 4 and len(recent) == 2

    async def test_end_to_end_compressed_tool_conversation_is_valid(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": long_text()}, {"role": "assistant", "content": long_text()},
                {"role": "user", "content": "use the tool"}, call(1), reply(1),
                {"role": "assistant", "content": "done"}]
        out, meta = await compress_context_with_metadata(msgs, "gpt-4o", proxy(), SETTINGS)
        assert meta["compressed"]
        calls = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
        assert all(m["tool_call_id"] in calls for m in out if m["role"] == "tool")
        assert out[0]["role"] == "system"


class TestMemoisation:
    MSGS = [{"role": "user", "content": long_text()}, {"role": "assistant", "content": long_text()},
            {"role": "user", "content": long_text()}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]

    async def test_second_identical_conversation_does_not_call_the_summariser(self):
        p = proxy()
        first, _ = await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        second, _ = await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        assert p.simple_completion.await_count == 1
        assert first == second

    async def test_growing_conversation_with_new_early_turns_resummarises(self):
        p = proxy()
        await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        changed = [{"role": "user", "content": "different start " * 40}] + self.MSGS[1:]
        await compress_context_with_metadata(changed, "gpt-4o", p, SETTINGS)
        assert p.simple_completion.await_count == 2

    async def test_memo_is_per_summary_model(self):
        p = proxy()
        await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        other = Settings(**{**SETTINGS.model_dump(), "compress_summary_model": "other-model"})
        await compress_context_with_metadata(self.MSGS, "gpt-4o", p, other)
        assert p.simple_completion.await_count == 2


class TestFallbacks:
    MSGS = TestMemoisation.MSGS

    async def test_summariser_error_returns_original_messages(self):
        p = AsyncMock()
        p.simple_completion.side_effect = RuntimeError("provider down")
        out, meta = await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        assert out == self.MSGS and meta["compressed"] is False

    async def test_empty_summary_is_rejected_and_not_memoised(self):
        p = proxy(summary="   ")
        out, meta = await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        assert out == self.MSGS and not meta["compressed"]
        p.simple_completion.return_value = {"choices": [{"message": {"content": "real summary"}}]}
        _, meta2 = await compress_context_with_metadata(self.MSGS, "gpt-4o", p, SETTINGS)
        assert meta2["compressed"] and p.simple_completion.await_count == 2

    async def test_short_conversations_are_untouched(self):
        p = proxy()
        short = [{"role": "user", "content": "hi"}]
        out, meta = await compress_context_with_metadata(short, "gpt-4o", p, SETTINGS)
        assert out == short and not meta["compressed"]
        p.simple_completion.assert_not_awaited()
