"""LiteLLM Router failover and caching behaviour.

Tests:
  - ProxyClient initialises a Router with the correct model list
  - 429 from primary provider is mapped to UpstreamError(429)
  - Router retries: on 1st-call failure, 2nd call succeeds (fallback)
  - LiteLLM in-memory cache: second identical request skips the API
  - Cache hit delivers near-zero extra latency
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from app.config import Settings
from app.proxy import ProxyClient, UpstreamError

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_settings(**overrides) -> Settings:
    """Return a Settings instance pre-seeded with dummy keys."""
    defaults = dict(
        openai_api_key="sk-openai-test",
        anthropic_api_key="sk-ant-test",
        groq_api_key="gsk-groq-test",
        gemini_api_key="AIza-gemini-test",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _mock_response(content: str = "Hello!", model: str = "gpt-3.5-turbo-0125") -> MagicMock:
    """Return a MagicMock that looks like a litellm ModelResponse."""
    resp = MagicMock()
    resp.model_dump.return_value = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop", "index": 0}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return resp


# ── Router Initialisation ─────────────────────────────────────────────────


class TestRouterInit:
    """Verify the LiteLLM Router is built correctly from settings."""

    def test_router_created(self):
        """ProxyClient should expose a litellm.Router instance."""
        client = ProxyClient(_make_settings())
        assert isinstance(client.router, litellm.Router)

    def test_model_list_populated_when_keys_set(self):
        """Model list should contain entries for every provider key that is set."""
        client = ProxyClient(_make_settings())
        names = {e["model_name"] for e in client.router.model_list}
        assert "simple-tier" in names
        assert "complex-tier" in names
        assert "gpt-3.5-turbo" in names
        assert "gpt-4o" in names
        assert "claude-3-5-sonnet" in names

    def test_model_list_empty_keys_excluded(self):
        """Deployments for providers without keys should be absent."""
        client = ProxyClient(_make_settings(groq_api_key="", gemini_api_key=""))
        models = [e["litellm_params"]["model"] for e in client.router.model_list]
        assert not any("groq" in m for m in models)
        assert not any("gemini" in m for m in models)

    def test_fallback_map_built_when_keys_available(self):
        """Fallback chains should reference providers whose keys are set."""
        fallbacks = ProxyClient._build_fallback_map(_make_settings())
        # at least gpt-4o → claude/gemini should be present
        gpt4o_chain = next((f["gpt-4o"] for f in fallbacks if "gpt-4o" in f), None)
        assert gpt4o_chain is not None
        assert "claude-3-5-sonnet" in gpt4o_chain


# ── Failover Tests ────────────────────────────────────────────────────────


class TestFailover:
    """Verify the Router handles upstream failures and retries correctly."""

    @pytest.mark.asyncio
    async def test_rate_limit_mapped_to_upstream_error_429(self):
        """A 429 bubbling out of the Router should become UpstreamError(429)."""
        client = ProxyClient(_make_settings())

        exc = Exception("Rate limit exceeded. Please retry after 60s.")
        exc.status_code = 429  # type: ignore[attr-defined]

        with patch.object(client.router, "acompletion", new_callable=AsyncMock) as mock:
            mock.side_effect = exc
            with pytest.raises(UpstreamError) as exc_info:
                await client.simple_completion(
                    messages=[{"role": "user", "content": "test"}],
                    model="gpt-3.5-turbo",
                )
            assert exc_info.value.status_code == 429
            assert "Rate limit" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_502_connection_error_mapped(self):
        """Connection errors (502) are wrapped in UpstreamError(502)."""
        client = ProxyClient(_make_settings())

        exc = Exception("Upstream connection refused")
        exc.status_code = 502  # type: ignore[attr-defined]

        with patch.object(client.router, "acompletion", new_callable=AsyncMock) as mock:
            mock.side_effect = exc
            with pytest.raises(UpstreamError) as exc_info:
                await client.simple_completion(
                    messages=[{"role": "user", "content": "ping"}],
                    model="gpt-4o",
                )
            assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_failover_succeeds_on_first_call_failure(self):
        """Simulates Router-level failover: primary raises 429, caller retries, succeeds."""
        client = ProxyClient(_make_settings())

        call_count = 0

        async def mock_acompletion(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                exc = Exception("OpenAI rate limit")
                exc.status_code = 429  # type: ignore[attr-defined]
                raise exc
            # Second invocation returns a successful response (fallback provider)
            return _mock_response(content="Fallback response", model="claude-3-5-sonnet-20241022")

        # First call hits the 429 → UpstreamError
        with patch.object(client.router, "acompletion", side_effect=mock_acompletion):
            with pytest.raises(UpstreamError) as exc_info:
                await client.simple_completion(
                    messages=[{"role": "user", "content": "Hello"}],
                    model="gpt-3.5-turbo",
                )
            assert exc_info.value.status_code == 429

            # Second call (simulates caller retrying after failover) succeeds
            result = await client.simple_completion(
                messages=[{"role": "user", "content": "Hello"}],
                model="gpt-3.5-turbo",
            )

        assert result["choices"][0]["message"]["content"] == "Fallback response"
        assert call_count == 2, f"Expected 2 total calls, got {call_count}"

    @pytest.mark.asyncio
    async def test_forward_uses_router(self):
        """forward() should call self.router.acompletion, not litellm.acompletion."""
        from app.models import ChatCompletionRequest, ChatMessage

        client = ProxyClient(_make_settings())
        mock_resp = _mock_response()

        with patch.object(client.router, "acompletion", new_callable=AsyncMock) as mock:
            mock.return_value = mock_resp
            request = ChatCompletionRequest(
                model="gpt-3.5-turbo",
                messages=[ChatMessage(role="user", content="Hi")],
            )
            result = await client.forward(request)

        mock.assert_called_once()
        assert result["choices"][0]["message"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_resolve_model_prefers_registered_names(self):
        """_resolve_model should pick router-registered names for known models."""
        client = ProxyClient(_make_settings())
        assert client._resolve_model("gpt-4o") == "gpt-4o"       # registered
        assert client._resolve_model("gpt-3.5-turbo") == "gpt-3.5-turbo"  # registered


# ── Cache Tests ───────────────────────────────────────────────────────────
