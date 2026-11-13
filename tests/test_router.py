"""Unit and integration tests for the model router.

Tests the rule-based complexity classifier against a labeled prompt set,
verifies override header behavior, and checks endpoint integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.router import ModelRouter, RoutingDecision, Tier

BENCHMARKS_DIR = Path(__file__).parent.parent / "benchmarks"


# ──────────────────────── Router Unit Tests ──────────────────────────────


class TestModelRouter:
    """Tests for the rule-based complexity classifier."""

    @pytest.fixture
    def router(self) -> ModelRouter:
        """Return a real ModelRouter with the project's routing config."""
        return ModelRouter(config_path="config/routing_rules.yaml", preferred_provider="openai")

    def test_simple_greeting(self, router):
        """Greetings should route to SIMPLE."""
        result = router.route("gpt-3.5-turbo", [{"role": "user", "content": "Hello!"}])
        assert result.tier == Tier.SIMPLE
        assert result.model_selected == "gpt-3.5-turbo"

    def test_complex_analysis(self, router):
        """Analysis requests should route to COMPLEX."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "Analyze the time complexity of quicksort."}],
        )
        assert result.tier == Tier.COMPLEX
        assert result.model_selected == "gpt-4o"

    def test_complex_debug(self, router):
        """Debug requests should route to COMPLEX."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "Debug this Python function that crashes."}],
        )
        assert result.tier == Tier.COMPLEX

    def test_complex_refactor(self, router):
        """Refactor requests should route to COMPLEX."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "Refactor this code to use dependency injection."}],
        )
        assert result.tier == Tier.COMPLEX

    def test_simple_factual(self, router):
        """Short factual questions should route to SIMPLE."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "What is the capital of France?"}],
        )
        assert result.tier == Tier.SIMPLE

    def test_override_forces_model(self, router):
        """Override header forces the specified model regardless of classification."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "Hello!"}],
            override_model="gpt-4o",
        )
        assert result.model_selected == "gpt-4o"
        assert result.override is True
        assert result.reason == "header_override"

    def test_anthropic_provider(self):
        """Anthropic provider maps to claude models."""
        router = ModelRouter(config_path="config/routing_rules.yaml", preferred_provider="anthropic")
        result = router.route(
            "claude-3-haiku",
            [{"role": "user", "content": "Analyze this complex system architecture."}],
        )
        assert result.tier == Tier.COMPLEX
        assert result.model_selected == "claude-3-5-sonnet-20241022"

    def test_token_counting(self, router):
        """Token count is included in the routing decision."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "Hello!"}],
        )
        assert result.token_count > 0

    def test_routing_decision_fields(self, router):
        """All fields in RoutingDecision are populated."""
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": "Hello!"}],
        )
        assert isinstance(result, RoutingDecision)
        assert result.model_requested == "gpt-3.5-turbo"
        assert result.reason != ""
        assert result.tier in (Tier.SIMPLE, Tier.COMPLEX)

    def test_ambiguous_defaults_to_complex(self, router):
        """Mid-length prompts without clear signals should default to COMPLEX."""
        mid_length = "The quick brown fox jumps over the lazy dog. " * 20
        result = router.route(
            "gpt-3.5-turbo",
            [{"role": "user", "content": mid_length}],
        )
        assert result.tier == Tier.COMPLEX
        assert result.reason == "ambiguous_default"


# ──────────────────── Labeled Prompt Accuracy ────────────────────────────


class TestLabeledPromptAccuracy:
    """Validate routing accuracy against the 1000-prompt labeled test set."""

    @pytest.fixture
    def labeled_prompts(self) -> list[dict]:
        """Load the labeled prompt set."""
        return json.loads((BENCHMARKS_DIR / "prompts_labeled.json").read_text())

    @pytest.fixture
    def router(self) -> ModelRouter:
        return ModelRouter(config_path="config/routing_rules.yaml", preferred_provider="openai")

    def test_accuracy_above_85_percent(self, router, labeled_prompts):
        """Router must correctly classify ≥85% of the labeled prompts."""
        correct = 0
        errors = []

        for item in labeled_prompts:
            prompt = item["prompt"]
            expected = item["label"]  # "simple" or "complex"
            result = router.route("gpt-3.5-turbo", [{"role": "user", "content": prompt}])

            if result.tier.value == expected:
                correct += 1
            else:
                errors.append(
                    f"  WRONG: '{prompt[:60]}...' expected={expected} got={result.tier.value} reason={result.reason}"
                )

        accuracy = correct / len(labeled_prompts)
        assert accuracy >= 0.85, (
            f"Accuracy {accuracy:.1%} is below 85% ({correct}/{len(labeled_prompts)}). "
            f"Misclassified (first 20):\n" + "\n".join(errors[:20])
        )

    def test_simple_accuracy_above_80_percent(self, router, labeled_prompts):
        """At least 80% of simple-labeled prompts are classified correctly."""
        simple_prompts = [p for p in labeled_prompts if p["label"] == "simple"]
        correct = sum(
            1
            for p in simple_prompts
            if router.route("gpt-3.5-turbo", [{"role": "user", "content": p["prompt"]}]).tier == Tier.SIMPLE
        )
        accuracy = correct / len(simple_prompts)
        assert accuracy >= 0.80, f"Simple accuracy: {accuracy:.1%} ({correct}/{len(simple_prompts)})"

    def test_complex_accuracy_above_90_percent(self, router, labeled_prompts):
        """At least 90% of complex-labeled prompts are classified correctly."""
        complex_prompts = [p for p in labeled_prompts if p["label"] == "complex"]
        correct = sum(
            1
            for p in complex_prompts
            if router.route("gpt-3.5-turbo", [{"role": "user", "content": p["prompt"]}]).tier == Tier.COMPLEX
        )
        accuracy = correct / len(complex_prompts)
        assert accuracy >= 0.90, f"Complex accuracy: {accuracy:.1%} ({correct}/{len(complex_prompts)})"

    def test_dataset_has_1000_entries(self, labeled_prompts):
        """Verify the dataset has at least 1000 entries."""
        assert len(labeled_prompts) >= 1000, f"Expected ≥1000 prompts, got {len(labeled_prompts)}"

    def test_dataset_category_distribution(self, labeled_prompts):
        """Verify all 8 categories are represented with at least 100 entries each."""
        from collections import Counter

        cats = Counter(p["category"] for p in labeled_prompts)
        expected_cats = {
            "qa", "code", "creative", "analytical",
            "conversational", "summarization", "debug", "comparison",
        }
        assert set(cats.keys()) == expected_cats, f"Missing categories: {expected_cats - set(cats.keys())}"
        for cat, count in cats.items():
            assert count >= 100, f"Category '{cat}' has only {count} entries (need ≥100)"


# ────────────── Integration: Router in Chat Endpoint ─────────────────────


class TestChatCompletionsWithRouter:
    """Tests for model routing in the /v1/chat/completions endpoint."""

    def test_routing_headers_present(
        self, test_client, mock_proxy_client, mock_cache, chat_completion_fixture, sample_request_body
    ):
        """Response includes X-Model-Tier and X-Model-Selected headers."""
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)
        assert resp.status_code == 200
        assert "x-model-tier" in resp.headers
        assert "x-model-selected" in resp.headers

    def test_override_header_forces_model(
        self, test_client, mock_router, mock_proxy_client, mock_cache, chat_completion_fixture, sample_request_body
    ):
        """X-NightJar-Model-Override header forces the specified model."""
        mock_router.route.return_value = RoutingDecision(
            tier=Tier.COMPLEX,
            model_requested="gpt-3.5-turbo",
            model_selected="gpt-4o",
            reason="header_override",
            token_count=2,
            override=True,
        )
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post(
            "/v1/chat/completions",
            json=sample_request_body,
            headers={"X-NightJar-Model-Override": "gpt-4o"},
        )

        assert resp.status_code == 200
        assert resp.headers.get("x-model-selected") == "gpt-4o"
        mock_router.route.assert_called_once()
        call_kwargs = mock_router.route.call_args
        assert call_kwargs[1].get("override_model") == "gpt-4o" or call_kwargs[0][-1] == "gpt-4o"

    def test_routed_model_passed_to_proxy(
        self, test_client, mock_router, mock_proxy_client, mock_cache, chat_completion_fixture, sample_request_body
    ):
        """The router's selected model is passed to the proxy client."""
        mock_router.route.return_value = RoutingDecision(
            tier=Tier.COMPLEX,
            model_requested="gpt-3.5-turbo",
            model_selected="gpt-4o",
            reason="complex_keyword:analyze",
            token_count=10,
        )
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)
        assert resp.status_code == 200

        mock_proxy_client.forward.assert_called_once()
        call_kwargs = mock_proxy_client.forward.call_args
        assert call_kwargs[1].get("model_override") == "gpt-4o" or (
            len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "gpt-4o"
        )

    def test_routing_decision_persisted_to_telemetry(
        self, test_client, mock_router, mock_proxy_client, mock_cache, chat_completion_fixture, sample_request_body
    ):
        """The routing tier and reason end up on the telemetry row, not just
        the response headers — this is what lets the dashboard show a real
        simple/complex breakdown instead of inventing one."""
        from app.telemetry import get_records

        mock_router.route.return_value = RoutingDecision(
            tier=Tier.COMPLEX,
            model_requested="gpt-3.5-turbo",
            model_selected="gpt-4o",
            reason="complex_keyword:analyze",
            token_count=10,
        )
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)
        assert resp.status_code == 200

        record = get_records(limit=1)[0]
        assert record["tier"] == "complex"
        assert record["routing_reason"] == "complex_keyword:analyze"
