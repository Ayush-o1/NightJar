"""The cache as callers experience it: HTTP in, headers and upstream calls out. Real Redis, mocked provider."""

from __future__ import annotations

import asyncio
import copy

import httpx
import pytest

from app import telemetry as tel
from app.cache import ResponseCache
from app.config import Settings
from app.main import app
from app.proxy import UpstreamError
from app.router import ModelRouter
from app.vector_index import VectorIndex
from tests.conftest import FakeEmbedder

BODY = {"model": "gpt-4o", "temperature": 0, "messages": [{"role": "user", "content": "what is 6*7?"}]}


@pytest.fixture
def wired(redis_client, mock_proxy_client, chat_completion_fixture):
    """The real app wired to a real ResponseCache and a mocked provider that counts its calls."""
    settings = Settings(openai_api_key="x")
    app.state.settings = settings
    app.state.router = ModelRouter(config_path="config/routing_rules.yaml", routing_mode="explicit")
    app.state.cache = ResponseCache(None, VectorIndex(FakeEmbedder.dimension), redis_client, settings)
    app.state.proxy_client = mock_proxy_client
    mock_proxy_client.forward.return_value = chat_completion_fixture
    return mock_proxy_client


@pytest.fixture
async def client(wired):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


pytestmark = pytest.mark.asyncio


async def post(client, body=BODY, **headers):
    return await client.post("/v1/chat/completions", json=body, headers=headers)


class TestExactCaching:
    async def test_second_identical_request_is_served_from_cache(self, client, wired):
        first = await post(client)
        second = await post(client)
        assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
        assert second.headers["x-cache-tier"] == "exact" and second.headers["x-similarity"] == "1.0000"
        assert second.json() == first.json()
        assert wired.forward.await_count == 1

    @pytest.mark.parametrize("change", [
        dict(model="gpt-4o-mini"), dict(temperature=0.3), dict(max_tokens=20),
        dict(messages=[{"role": "system", "content": "be terse"}, {"role": "user", "content": "what is 6*7?"}]),
        dict(messages=[{"role": "user", "content": "what is 6*8?"}]),
    ])
    async def test_any_difference_is_a_miss(self, client, wired, change):
        await post(client)
        resp = await post(client, {**BODY, **change})
        assert resp.headers["x-cache"] == "MISS"
        assert wired.forward.await_count == 2

    async def test_tool_requests_bypass_the_cache_with_a_reason(self, client, wired):
        body = {**BODY, "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}
        for _ in range(2):
            resp = await post(client, body)
            assert resp.headers["x-cache"] == "BYPASS" and resp.headers["x-cache-reason"] == "ineligible:tools"
        assert wired.forward.await_count == 2

    async def test_high_temperature_requests_are_not_cached(self, client, wired):
        for _ in range(2):
            assert (await post(client, {**BODY, "temperature": 1.8})).headers["x-cache"] == "BYPASS"
        assert wired.forward.await_count == 2

    async def test_incomplete_provider_response_is_not_cached(self, client, wired, chat_completion_fixture):
        truncated = copy.deepcopy(chat_completion_fixture)
        truncated["choices"][0]["finish_reason"] = "length"
        wired.forward.return_value = truncated
        await post(client)
        assert (await post(client)).headers["x-cache"] == "MISS"
        assert wired.forward.await_count == 2


class TestClientControl:
    async def test_bypass_header_skips_lookup_and_store(self, client, wired):
        r1 = await post(client, **{"X-NightJar-Cache": "bypass"})
        assert r1.headers["x-cache"] == "BYPASS" and r1.headers["x-cache-reason"] == "client_bypass"
        assert (await post(client)).headers["x-cache"] == "MISS"  # nothing was stored by the bypassed call

    async def test_refresh_header_skips_lookup_but_updates_the_entry(self, client, wired, chat_completion_fixture):
        await post(client)
        newer = copy.deepcopy(chat_completion_fixture)
        newer["choices"][0]["message"]["content"] = "fresh"
        wired.forward.return_value = newer
        refreshed = await post(client, **{"X-NightJar-Cache": "refresh"})
        assert refreshed.headers["x-cache"] == "MISS"
        hit = await post(client)
        assert hit.headers["x-cache"] == "HIT" and hit.json()["choices"][0]["message"]["content"] == "fresh"


class TestSingleFlight:
    async def test_concurrent_identical_requests_make_one_provider_call(self, client, wired, chat_completion_fixture):
        async def slow(*a, **k):
            await asyncio.sleep(0.2)
            return chat_completion_fixture

        wired.forward.side_effect = slow
        results = await asyncio.gather(*[post(client) for _ in range(8)])
        assert wired.forward.await_count == 1
        assert [r.status_code for r in results] == [200] * 8
        tiers = sorted(r.headers.get("x-cache-tier", "leader") for r in results)
        assert tiers.count("leader") == 1 and tiers.count("coalesced") == 7
        assert all(r.json() == results[0].json() for r in results)

    async def test_different_requests_do_not_wait_for_each_other(self, client, wired, chat_completion_fixture):
        async def slow(*a, **k):
            await asyncio.sleep(0.2)
            return chat_completion_fixture

        wired.forward.side_effect = slow
        bodies = [{**BODY, "messages": [{"role": "user", "content": f"q{i}"}]} for i in range(5)]
        start = asyncio.get_running_loop().time()
        await asyncio.gather(*[post(client, b) for b in bodies])
        assert wired.forward.await_count == 5
        assert asyncio.get_running_loop().time() - start < 0.6  # parallel, not 5 x 0.2

    async def test_leader_failure_is_shared_not_retried_by_every_follower(self, client, wired):
        async def failing(*a, **k):
            await asyncio.sleep(0.1)
            raise UpstreamError(429, "slow down")

        wired.forward.side_effect = failing
        results = await asyncio.gather(*[post(client) for _ in range(6)])
        assert [r.status_code for r in results] == [429] * 6
        assert wired.forward.await_count == 1, "followers must not each retry a failing provider"

    async def test_failure_is_not_cached(self, client, wired, chat_completion_fixture):
        wired.forward.side_effect = [UpstreamError(500, "boom"), chat_completion_fixture]
        assert (await post(client)).status_code == 502
        assert (await post(client)).status_code == 200


class TestDegradation:
    async def test_redis_down_means_every_request_is_a_paid_miss_not_an_error(self, client, wired):
        class Broken:
            async def get(self, *a, **k):
                raise ConnectionError("redis down")
            set = scan = delete = get

        app.state.cache = ResponseCache(None, VectorIndex(FakeEmbedder.dimension), Broken(), Settings())
        for _ in range(2):
            resp = await post(client)
            assert resp.status_code == 200 and resp.headers["x-cache"] == "MISS"
        assert wired.forward.await_count == 2


class TestTelemetryOnHits:
    async def test_hit_records_real_token_counts_and_zero_cost(self, client, wired, chat_completion_fixture):
        await post(client, {**BODY, "messages": [{"role": "user", "content": "telemetry probe"}]})
        await post(client, {**BODY, "messages": [{"role": "user", "content": "telemetry probe"}]})
        miss, hit = tel.get_records(2)[1], tel.get_records(2)[0]
        usage = chat_completion_fixture["usage"]
        assert hit["cache_hit"] and hit["prompt_tokens"] == usage["prompt_tokens"] > 0
        assert hit["estimated_cost_usd"] == 0.0 and miss["estimated_cost_usd"] > 0

    async def test_savings_are_no_longer_always_zero(self, client, wired):
        before = tel.get_total_savings()["cache_savings_usd"]
        for _ in range(3):
            await post(client, {**BODY, "messages": [{"role": "user", "content": "savings probe"}]})
        assert tel.get_total_savings()["cache_savings_usd"] > before


class TestRemovedAdaptiveEndpoints:
    @pytest.mark.parametrize(("method", "path"), [
        ("GET", "/v1/threshold"), ("GET", "/v1/threshold/history"), ("POST", "/v1/threshold/evaluate"),
    ])
    async def test_answer_410_with_an_explanation(self, client, method, path):
        resp = await client.request(method, path)
        assert resp.status_code == 410
        err = resp.json()["error"]
        assert err["code"] == "endpoint_removed" and "SIMILARITY_THRESHOLD" in err["message"]


class TestStatsEndpoint:
    async def test_reports_counters_and_threshold(self, client, wired):
        await post(client)
        await post(client)
        stats = (await client.get("/v1/cache/stats")).json()
        assert stats["redis_keys"] == 1 and stats["semantic_enabled"] is False
        assert stats["counters"]["exact_hits"] == 1 and stats["similarity_threshold"] == 0.92
