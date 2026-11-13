"""ResponseCache against a real Redis, with a controllable embedder."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.cache import KEY_PREFIX, FlightAborted, ResponseCache
from app.config import Settings
from app.fingerprint import build_fingerprint
from app.vector_index import VectorIndex
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.asyncio


def answer(text="42", finish="stop", **extra):
    return {
        "id": "x",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}, **extra,
    }


def request(text, model="gpt-4o", system="s", **params):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    return build_fingerprint({"model": model, "messages": msgs, **params}, msgs, model)


def make_cache(redis, embedder=None, semantic=False, **settings):
    s = Settings(semantic_cache=semantic, similarity_threshold=0.9, **settings)
    idx = VectorIndex(FakeEmbedder.dimension)
    return ResponseCache(embedder, idx, redis, s), idx


class TestExactTier:
    async def test_store_then_hit(self, redis_client):
        cache, _ = make_cache(redis_client)
        fp = request("what is 6*7?")
        assert (await cache.lookup(fp)).hit is False
        assert await cache.store(fp, answer())
        hit = await cache.lookup(fp)
        assert hit.hit and hit.tier == "exact" and hit.similarity_score == 1.0
        assert hit.response["choices"][0]["message"]["content"] == "42"

    @pytest.mark.parametrize("variant", [
        dict(model="gpt-4o-mini"), dict(temperature=0.1), dict(max_tokens=9), dict(system="other"),
    ])
    async def test_different_model_params_or_system_prompt_never_hit(self, redis_client, variant):
        cache, _ = make_cache(redis_client)
        await cache.store(request("q", temperature=0.5), answer())
        base = dict(temperature=0.5)
        base.update({k: v for k, v in variant.items() if k not in ("model", "system")})
        other = request("q", model=variant.get("model", "gpt-4o"), system=variant.get("system", "s"), **base)
        assert (await cache.lookup(other)).hit is False

    async def test_ttl_is_applied(self, redis_client):
        cache, _ = make_cache(redis_client, cache_ttl_seconds=100)
        fp = request("q")
        await cache.store(fp, answer())
        assert 0 < await redis_client.ttl(KEY_PREFIX + fp.exact_key) <= 100

    async def test_ineligible_requests_are_neither_served_nor_stored(self, redis_client):
        cache, _ = make_cache(redis_client)
        fp = request("q", tools=[{"type": "function"}])
        assert await cache.store(fp, answer()) is False
        assert await redis_client.dbsize() == 0
        assert (await cache.lookup(fp)).reason == "ineligible:tools"

    @pytest.mark.parametrize("response", [answer(finish="length"), answer(text=""), answer(finish="tool_calls")])
    async def test_incomplete_responses_are_not_stored(self, redis_client, response):
        cache, _ = make_cache(redis_client)
        assert await cache.store(request("q"), response) is False
        assert await redis_client.dbsize() == 0

    async def test_corrupt_payload_is_a_miss_not_a_crash(self, redis_client):
        cache, _ = make_cache(redis_client)
        fp = request("q")
        await redis_client.set(KEY_PREFIX + fp.exact_key, "{not json")
        assert (await cache.lookup(fp)).hit is False


class TestSemanticTier:
    async def setup_pair(self, redis_client, emb, similarity, a="How do I reset my password?",
                         b="How can I reset my password?", semantic=True):
        va, vb = FakeEmbedder.pair(similarity)
        emb.set(a, va), emb.set(b, vb)
        cache, idx = make_cache(redis_client, emb, semantic=semantic)
        await cache.store(request(a), answer("steps..."))
        return cache, idx, request(b)

    async def test_hit_above_threshold(self, redis_client, fake_embedder):
        cache, _, probe = await self.setup_pair(redis_client, fake_embedder, 0.95)
        hit = await cache.lookup(probe)
        assert hit.hit and hit.tier == "semantic" and hit.similarity_score == pytest.approx(0.95, abs=1e-3)

    async def test_miss_below_threshold(self, redis_client, fake_embedder):
        cache, _, probe = await self.setup_pair(redis_client, fake_embedder, 0.85)
        miss = await cache.lookup(probe)
        assert not miss.hit and miss.reason == "below_threshold"
        assert miss.similarity_score == pytest.approx(0.85, abs=1e-3)

    async def test_off_by_default_even_when_embedder_present(self, redis_client, fake_embedder):
        cache, idx, probe = await self.setup_pair(redis_client, fake_embedder, 0.99, semantic=False)
        assert not (await cache.lookup(probe)).hit and len(idx) == 0

    async def test_guard_rejects_a_similar_but_different_question(self, redis_client, fake_embedder):
        cache, _, probe = await self.setup_pair(
            redis_client, fake_embedder, 0.97, a="What was the revenue in 2022?", b="What was the revenue in 2023?")
        miss = await cache.lookup(probe)
        assert not miss.hit and miss.reason == "guard:numbers"
        assert cache.counters["guard_rejections"] == 1

    async def test_never_crosses_partitions(self, redis_client, fake_embedder):
        """Same words, same similarity, but different model / system prompt / params: no hit."""
        va, vb = FakeEmbedder.pair(0.99)
        fake_embedder.set("hello there", va), fake_embedder.set("hello there friend", vb)
        cache, _ = make_cache(redis_client, fake_embedder, semantic=True)
        await cache.store(request("hello there"), answer())
        for probe in (request("hello there friend", model="gpt-4o-mini"),
                      request("hello there friend", system="other"),
                      request("hello there friend", temperature=0.2)):
            assert not (await cache.lookup(probe)).hit
        assert (await cache.lookup(request("hello there friend"))).hit

    async def test_dangling_vector_is_removed_when_payload_expired(self, redis_client, fake_embedder):
        cache, idx, probe = await self.setup_pair(redis_client, fake_embedder, 0.95)
        await redis_client.flushdb()  # payload gone, vector remains
        assert len(idx) == 1
        assert (await cache.lookup(probe)).reason == "payload_missing"
        assert len(idx) == 0

    async def test_embedder_failure_degrades_to_miss(self, redis_client, fake_embedder):
        cache, _, probe = await self.setup_pair(redis_client, fake_embedder, 0.95)
        fake_embedder.fail = True
        miss = await cache.lookup(probe)
        assert not miss.hit and miss.reason == "embed_error" and cache.counters["embed_errors"] == 1

    async def test_embedder_failure_on_store_still_stores_the_exact_entry(self, redis_client, fake_embedder):
        cache, idx = make_cache(redis_client, fake_embedder, semantic=True)
        fake_embedder.fail = True
        fp = request("q")
        assert await cache.store(fp, answer())
        assert len(idx) == 0 and (await cache.lookup(fp)).hit

    async def test_media_prompt_uses_exact_tier_only(self, redis_client, fake_embedder):
        cache, idx = make_cache(redis_client, fake_embedder, semantic=True)
        msgs = [{"role": "user", "content": [{"type": "text", "text": "x"}, {"type": "image_url", "image_url": {}}]}]
        fp = build_fingerprint({}, msgs, "gpt-4o")
        await cache.store(fp, answer())
        assert len(idx) == 0 and (await cache.lookup(fp)).hit


class TestRedisFailure:
    class Broken:
        async def get(self, *a, **k):
            raise ConnectionError("down")

        set = delete = scan = get

        async def aclose(self):
            pass

    async def test_every_operation_degrades_instead_of_raising(self):
        cache, _ = make_cache(self.Broken())
        fp = request("q")
        assert (await cache.lookup(fp)).reason == "redis_error"
        assert await cache.store(fp, answer()) is False
        assert await cache.invalidate("k") is False
        assert await cache.flush() == {"vectors_cleared": 0, "redis_keys_cleared": 0}
        assert (await cache.stats())["redis_keys"] == 0
        assert cache.counters["redis_errors"] >= 4

    async def test_no_vector_is_indexed_when_the_payload_could_not_be_stored(self, fake_embedder):
        cache, idx = make_cache(self.Broken(), fake_embedder, semantic=True)
        await cache.store(request("q"), answer())
        assert len(idx) == 0


class TestAdmin:
    async def test_invalidate_and_flush(self, redis_client, fake_embedder):
        cache, idx = make_cache(redis_client, fake_embedder, semantic=True)
        a, b = request("one"), request("two")
        await cache.store(a, answer()), await cache.store(b, answer())
        assert await cache.invalidate(a.exact_key) and not (await cache.lookup(a)).hit
        assert (await cache.lookup(b)).hit
        out = await cache.flush()
        assert out == {"vectors_cleared": 1, "redis_keys_cleared": 1}
        stats = await cache.stats()
        assert stats["total_vectors"] == 0 and stats["redis_keys"] == 0

    async def test_only_nightjar_keys_are_flushed(self, redis_client):
        cache, _ = make_cache(redis_client)
        await redis_client.set("someone-elses-key", "keep")
        await cache.store(request("q"), answer())
        await cache.flush()
        assert await redis_client.get("someone-elses-key") == "keep"

    async def test_persist_and_reload_across_restart(self, redis_client, fake_embedder, tmp_path):
        path = str(tmp_path / "v.npz")
        cache, idx = make_cache(redis_client, fake_embedder, semantic=True, vector_index_path=path)
        va, vb = FakeEmbedder.pair(0.97)
        fake_embedder.set("How do I reset my password?", va), fake_embedder.set("How can I reset my password?", vb)
        await cache.store(request("How do I reset my password?"), answer("steps"))
        await cache.persist()
        cache2, idx2 = make_cache(redis_client, fake_embedder, semantic=True, vector_index_path=path)
        assert idx2.load(path) == 1
        assert (await cache2.lookup(request("How can I reset my password?"))).tier == "semantic"


class TestSingleFlight:
    async def test_followers_get_the_leaders_result(self, redis_client):
        cache, _ = make_cache(redis_client)
        leader = cache.begin_flight("k")
        follower = cache.begin_flight("k")
        assert leader.leader and not follower.leader
        waiting = asyncio.create_task(follower.wait())
        await asyncio.sleep(0)
        leader.finish(response={"ok": 1})
        assert await waiting == {"ok": 1}
        assert cache.begin_flight("k").leader  # slot is free again

    async def test_leader_error_reaches_followers(self, redis_client):
        cache, _ = make_cache(redis_client)
        leader, follower = cache.begin_flight("k"), cache.begin_flight("k")
        waiting = asyncio.create_task(follower.wait())
        await asyncio.sleep(0)
        leader.finish(error=ValueError("upstream failed"))
        with pytest.raises(ValueError, match="upstream failed"):
            await waiting

    async def test_cancelled_leader_releases_followers_to_retry(self, redis_client):
        cache, _ = make_cache(redis_client)
        leader, follower = cache.begin_flight("k"), cache.begin_flight("k")
        waiting = asyncio.create_task(follower.wait())
        await asyncio.sleep(0)
        leader.finish()  # no response, no error = aborted
        with pytest.raises(FlightAborted):
            await waiting

    async def test_leader_error_with_no_followers_logs_no_warning(self, redis_client, recwarn):
        cache, _ = make_cache(redis_client)
        cache.begin_flight("k").finish(error=RuntimeError("x"))
        await asyncio.sleep(0)
        assert not [w for w in recwarn if "never retrieved" in str(w.message)]


async def test_cache_payload_never_contains_the_api_key_or_extra_metadata(redis_client):
    cache, _ = make_cache(redis_client)
    fp = request("q")
    await cache.store(fp, answer())
    stored = json.loads(await redis_client.get(KEY_PREFIX + fp.exact_key))
    assert set(stored) == {"response", "prompt", "model", "created"}


class TestEmbeddingReuse:
    async def test_a_miss_followed_by_a_store_embeds_the_prompt_once(self, redis_client, fake_embedder):
        cache, idx = make_cache(redis_client, fake_embedder, semantic=True)
        fp = request("a brand new question")
        assert not (await cache.lookup(fp)).hit
        await cache.store(fp, answer())
        assert fake_embedder.calls == 1 and len(idx) == 1

    async def test_store_without_a_prior_lookup_still_embeds(self, redis_client, fake_embedder):
        cache, idx = make_cache(redis_client, fake_embedder, semantic=True)
        await cache.store(request("no lookup first"), answer())
        assert fake_embedder.calls == 1 and len(idx) == 1

    async def test_pending_vectors_are_bounded(self, redis_client, fake_embedder):
        cache, _ = make_cache(redis_client, fake_embedder, semantic=True)
        for i in range(400):
            await cache.lookup(request(f"never stored {i}"))
        assert len(cache._pending_vectors) <= 256
