"""Hostile and oversized input must be cheap to reject or bound. See docs/BASELINE.md (10 MB request)."""

from __future__ import annotations

import time

from app.config import Settings
from app.router import ModelRouter
from app.tokens import (
    MAX_EXACT_CHARS,
    content_to_text,
    count_message_tokens,
    count_text_tokens,
    total_text_chars,
)


class TestBoundedTokenCounting:
    def test_pathological_input_is_fast(self):
        """Baseline: 200 KB of 'a' took 8.6 s and 10 MB never finished. Now bounded by MAX_EXACT_CHARS."""
        for size in (200_000, 1_000_000, 10_000_000):
            start = time.perf_counter()
            tokens = count_text_tokens("a" * size)
            assert time.perf_counter() - start < 0.5, f"{size} chars took too long"
            assert tokens > 0

    def test_exact_below_cap_estimate_above(self):
        import tiktoken

        text = "hello world " * 100
        exact = count_text_tokens(text)
        assert exact == len(tiktoken.encoding_for_model("gpt-3.5-turbo").encode(text))
        big = count_text_tokens("hello world " * 100_000)
        assert big > exact * 100  # grows with size even when estimated

    def test_estimate_is_monotonic_across_the_cap(self):
        assert count_text_tokens("a" * (MAX_EXACT_CHARS + 4000)) >= count_text_tokens("a" * MAX_EXACT_CHARS)

    def test_router_survives_hostile_prompt_quickly(self):
        router = ModelRouter(config_path="config/routing_rules.yaml")
        start = time.perf_counter()
        decision = router.route("gpt-4o", [{"role": "user", "content": "a" * 5_000_000}])
        assert time.perf_counter() - start < 0.5
        assert decision.tier.value == "complex"  # huge prompts are complex by token count

    def test_message_tokens_include_tool_calls(self):
        plain = count_message_tokens([{"role": "assistant", "content": None}])
        with_call = count_message_tokens(
            [{"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}]
        )
        assert with_call > plain


class TestContentToText:
    def test_none_and_string(self):
        assert content_to_text(None) == ""
        assert content_to_text("hi") == "hi"

    def test_parts_text_joined_and_media_replaced_by_placeholder(self):
        parts = [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}},
            {"type": "text", "text": "here"},
        ]
        text = content_to_text(parts)
        assert text == "look\n[image_url]\nhere"
        assert len(text) < 100  # the base64 payload never becomes text

    def test_total_text_chars_ignores_image_payloads(self):
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "A" * 500_000}}]}]
        assert total_text_chars(msgs) < 20


class TestPromptBudgetOnTheApi:
    def test_over_budget_is_a_400_with_context_length_code(self, test_client):
        from app.main import app

        app.state.settings = Settings(max_prompt_chars=100)
        resp = test_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x" * 101}]},
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["code"] == "context_length_exceeded" and err["param"] == "messages"

    def test_at_budget_is_accepted(self, test_client, mock_proxy_client, chat_completion_fixture):
        from app.main import app

        app.state.settings = Settings(max_prompt_chars=100)
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = test_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x" * 100}]},
        )
        assert resp.status_code == 200

    def test_telemetry_limit_is_bounded(self, test_client):
        assert test_client.get("/v1/telemetry?limit=501").status_code == 400
        assert test_client.get("/v1/telemetry?limit=0").status_code == 400
        assert test_client.get("/v1/telemetry?limit=500").status_code == 200
