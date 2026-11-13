"""Failure behaviour against a fake provider over real HTTP, through the real LiteLLM Router.

Each scenario is something the baseline got wrong or never tested (docs/BASELINE.md, TECHNICAL_AUDIT.md E-1).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.cache import ResponseCache
from app.config import Settings
from app.limits import ConcurrencyLimiter
from app.main import app
from app.models import ChatCompletionRequest
from app.proxy import ProxyClient, UpstreamError
from app.router import ModelRouter
from app.vector_index import VectorIndex
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.asyncio


def req(text="hello", **kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(model="gpt-4o", messages=[{"role": "user", "content": text}], **kw)


def client_for(provider, **overrides) -> ProxyClient:
    settings = Settings(openai_api_key="sk-fake", upstream_retries=2, upstream_cooldown_seconds=30, **overrides)
    return ProxyClient(settings)


class TestErrorsThatMustNotBeRetried:
    async def test_provider_auth_failure_is_attempted_once(self, provider):
        proxy = client_for(provider)
        with pytest.raises(UpstreamError) as exc:
            await proxy.forward(req("[fail:401]"))
        assert exc.value.status_code == 401
        assert provider.stats["requests"] == 1, "retrying a 401 only adds latency"

    async def test_bad_request_is_attempted_once(self, provider):
        proxy = client_for(provider)
        with pytest.raises(UpstreamError) as exc:
            await proxy.forward(req("[fail:400]"))
        assert exc.value.status_code == 400
        assert provider.stats["requests"] == 1

    async def test_one_rejected_request_does_not_take_the_only_deployment_offline(self, provider):
        """Baseline: after a 401 the next request failed with 'No deployments available... try again in 5 seconds'."""
        proxy = client_for(provider)
        for _ in range(4):  # more than the default allowed-fails of 3
            with pytest.raises(UpstreamError):
                await proxy.forward(req("[fail:401]"))
        result = await proxy.forward(req("a perfectly fine request"))
        assert result["choices"][0]["message"]["content"].startswith("answer to:")


class TestErrorsThatAreRetriedBoundedly:
    @pytest.mark.parametrize("status", [500, 429])
    async def test_attempts_are_one_plus_configured_retries(self, provider, status):
        proxy = client_for(provider)
        with pytest.raises(UpstreamError) as exc:
            await proxy.forward(req(f"[fail:{status}]"))
        assert exc.value.status_code == status
        assert provider.stats["requests"] == 3, "1 attempt + upstream_retries(2), not unbounded"

    async def test_zero_retries_means_one_attempt(self, provider):
        proxy = ProxyClient(Settings(openai_api_key="sk-fake", upstream_retries=0))
        with pytest.raises(UpstreamError):
            await proxy.forward(req("[fail:500]"))
        assert provider.stats["requests"] == 1

    async def test_a_transient_failure_is_recovered_by_a_retry(self, provider):
        provider.config["fail_first_n"] = 1
        proxy = client_for(provider)
        result = await proxy.forward(req("transient"))
        assert result["choices"][0]["message"]["content"].startswith("answer to:")
        assert provider.stats["requests"] == 2 and provider.stats["failures"] == 1


class TestCircuitBreaker:
    """LiteLLM's cooldown acts as a circuit breaker: after repeated server-side failures the
    deployment is skipped for a while, so a sick provider is not hammered and callers fail fast."""

    async def test_repeated_5xx_opens_the_breaker_then_it_recovers(self, provider):
        proxy = ProxyClient(Settings(openai_api_key="sk-fake", upstream_retries=0, upstream_cooldown_seconds=1.0))
        for _ in range(4):
            with pytest.raises(UpstreamError):
                await proxy.forward(req("[fail:500]"))
        reached = provider.stats["requests"]
        assert reached <= 4
        start = time.perf_counter()
        with pytest.raises(UpstreamError):
            await proxy.forward(req("while the breaker is open"))
        assert provider.stats["requests"] == reached, "an open breaker must not call the provider"
        assert time.perf_counter() - start < 0.5, "and must fail fast"
        await asyncio.sleep(1.3)
        assert (await proxy.forward(req("after cooldown")))["choices"]


class TestTimeouts:
    async def test_a_hung_provider_is_cut_off_at_the_configured_timeout(self, provider):
        """LiteLLM's own default timeout is 6000 s. We set 60 s by default; here 0.5 s."""
        provider.config["latency_ms"] = 5000
        proxy = ProxyClient(Settings(openai_api_key="sk-fake", upstream_timeout_seconds=0.5, upstream_retries=0))
        start = time.perf_counter()
        with pytest.raises(UpstreamError) as exc:
            await proxy.forward(req())
        assert time.perf_counter() - start < 3
        assert exc.value.status_code in (408, 504)

    async def test_timeout_maps_to_504_through_the_api(self, provider):
        provider.config["latency_ms"] = 5000
        settings = Settings(openai_api_key="sk-fake", upstream_timeout_seconds=0.5, upstream_retries=0)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            resp = await c.post("/v1/chat/completions", json=body("timeout probe"))
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "upstream_timeout"

    def test_default_timeout_is_far_below_litellms_default(self):
        assert Settings().upstream_timeout_seconds <= 120


def body(text="hello", **kw):
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": text}], **kw}


def wire(settings, proxy, redis=None):
    app.state.settings = settings
    app.state.proxy_client = proxy
    app.state.router = ModelRouter(config_path="config/routing_rules.yaml", routing_mode="explicit")
    app.state.limiter = ConcurrencyLimiter(settings.max_concurrent_upstream)

    class NoRedis:
        async def get(self, *a, **k):
            raise ConnectionError("no redis in this test")
        set = scan = delete = get

    app.state.cache = ResponseCache(None, VectorIndex(FakeEmbedder.dimension), redis or NoRedis(), settings)


def api():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


class TestErrorMappingThroughTheApi:
    @pytest.mark.parametrize(("upstream", "status", "code"), [
        (401, 502, "upstream_auth_error"),
        (400, 400, "upstream_invalid_request"),
        (429, 429, "upstream_rate_limit"),
        (500, 502, "upstream_error"),
        (503, 503, "upstream_unavailable"),
    ])
    async def test_status_and_code(self, provider, upstream, status, code):
        settings = Settings(openai_api_key="sk-fake", upstream_retries=0)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            resp = await c.post("/v1/chat/completions", json=body(f"[fail:{upstream}]"))
        assert resp.status_code == status
        err = resp.json()["error"]
        assert err["code"] == code and resp.headers["x-request-id"]

    async def test_server_side_errors_do_not_echo_provider_text(self, provider):
        settings = Settings(openai_api_key="sk-fake", upstream_retries=0)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            resp = await c.post("/v1/chat/completions", json=body("[fail:500]"))
        assert "fake upstream failure" not in resp.text and "LiteLLM" not in resp.text

    async def test_streaming_connect_failure_is_a_real_http_error(self, provider):
        settings = Settings(openai_api_key="sk-fake", upstream_retries=0)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            resp = await c.post("/v1/chat/completions", json=body("[fail:429]", stream=True))
        assert resp.status_code == 429 and resp.headers["content-type"].startswith("application/json")

    async def test_streaming_happy_path_through_litellm(self, provider):
        settings = Settings(openai_api_key="sk-fake")
        wire(settings, ProxyClient(settings))
        async with api() as c:
            resp = await c.post("/v1/chat/completions", json=body("stream me", stream=True))
        assert resp.status_code == 200
        assert resp.text.rstrip().endswith("data: [DONE]")
        assert "answer" in resp.text


class TestLoadShedding:
    async def test_over_the_limit_is_503_with_retry_after_and_the_limit_recovers(self, provider):
        provider.config["latency_ms"] = 400
        settings = Settings(openai_api_key="sk-fake", max_concurrent_upstream=2)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            results = await asyncio.gather(*[c.post("/v1/chat/completions", json=body(f"distinct {i}"))
                                             for i in range(5)])
            codes = sorted(r.status_code for r in results)
            assert codes == [200, 200, 503, 503, 503]
            shed = next(r for r in results if r.status_code == 503)
            assert shed.headers["retry-after"] == "1" and shed.json()["error"]["code"] == "gateway_overloaded"
            assert provider.stats["requests"] == 2, "shed requests must never reach the provider"
            assert app.state.limiter.in_flight == 0
            assert (await c.post("/v1/chat/completions", json=body("after the burst"))).status_code == 200

    async def test_health_reports_shed_requests(self, provider):
        provider.config["latency_ms"] = 300
        settings = Settings(openai_api_key="sk-fake", max_concurrent_upstream=1)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            await asyncio.gather(*[c.post("/v1/chat/completions", json=body(f"q{i}")) for i in range(3)])
            health = (await c.get("/health")).json()
        assert health["upstream"]["rejected"] == 2 and health["upstream"]["limit"] == 1

    async def test_slot_is_released_after_a_provider_error(self, provider):
        settings = Settings(openai_api_key="sk-fake", max_concurrent_upstream=1, upstream_retries=0)
        wire(settings, ProxyClient(settings))
        async with api() as c:
            for _ in range(3):
                assert (await c.post("/v1/chat/completions", json=body("[fail:500]"))).status_code == 502
        assert app.state.limiter.in_flight == 0

    async def test_cache_hits_do_not_use_slots(self, provider, redis_client):
        settings = Settings(openai_api_key="sk-fake", max_concurrent_upstream=1)
        wire(settings, ProxyClient(settings), redis=redis_client)
        async with api() as c:
            await c.post("/v1/chat/completions", json=body("cache me", temperature=0))
            app.state.limiter.in_flight = 1  # saturate
            hit = await c.post("/v1/chat/completions", json=body("cache me", temperature=0))
            app.state.limiter.in_flight = 0
        assert hit.status_code == 200 and hit.headers["x-cache"] == "HIT"
