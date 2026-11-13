"""Response cache: an exact tier that is always safe, and an opt-in semantic tier behind guards.

Tier 1, exact (always on): the key is a hash of the full request fingerprint
(app/fingerprint.py): model, generation parameters, every message. A hit means
the request is byte-for-byte the same question to the same model with the same
settings.

Tier 2, semantic (``SEMANTIC_CACHE=true``): only searches entries in the *same
partition* (same model, parameters and conversation context; only the last user
message differs), requires similarity >= threshold, and then rejects the hit if
the two prompts disagree on numbers, negation, quoted text or proper nouns
(app/guards.py). This tier can still return a wrong answer for prompts that
differ in an ordinary lowercase word; see docs/benchmarks/CACHE_CORRECTNESS.md.

Storage: response payloads live in Redis (TTL). Vectors live in an in-process
index persisted to a local file. Redis and embedding failures degrade to a
miss; they never fail the request.

Single-flight: identical eligible requests that arrive while the first is still
running wait for it instead of each calling the provider (in-process only).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis

from app.config import Settings
from app.fingerprint import Fingerprint, response_is_cacheable
from app.guards import disagreement
from app.vector_index import VectorIndex

logger = structlog.get_logger()

KEY_PREFIX = "nj:v2:r:"
MAX_EMBED_CHARS = 4000


@dataclass
class CacheResult:
    """Result of a cache lookup. ``tier`` is "exact" or "semantic" on a hit."""

    hit: bool
    response: dict | None = None
    tier: str | None = None
    similarity_score: float = 0.0
    cache_key: str = ""
    reason: str = ""  # why it missed, for logs and debugging


class FlightAborted(Exception):
    """The request we were waiting on was cancelled; the follower should run on its own."""


class Flight:
    """Handle for one in-progress eligible request. The first caller is the leader."""

    def __init__(self, cache: ResponseCache, key: str, future: asyncio.Future, leader: bool) -> None:
        self._cache, self._key, self._future, self.leader = cache, key, future, leader

    async def wait(self) -> dict:
        """Followers: wait for the leader's response (or its exception)."""
        return await asyncio.shield(self._future)

    def finish(self, response: dict | None = None, error: BaseException | None = None) -> None:
        """Leader: publish the outcome to followers and deregister."""
        if not self.leader:
            return
        self._cache._flights.pop(self._key, None)
        if self._future.done():
            return
        if error is not None:
            self._future.set_exception(error)
        elif response is not None:
            self._future.set_result(response)
        else:
            self._future.set_exception(FlightAborted())


class ResponseCache:
    def __init__(self, embedder, index: VectorIndex, redis: Redis, settings: Settings) -> None:
        self._embedder = embedder  # None when the semantic tier is off
        self._index = index
        self._redis = redis
        self._settings = settings
        self._flights: dict[str, asyncio.Future] = {}
        # A miss embeds the prompt in lookup() and would embed it again in store(); one embedding is
        # ~5 ms (measured), so lookup leaves its vector here for store() to reuse. Small and bounded.
        self._pending_vectors: OrderedDict[str, object] = OrderedDict()
        self._maintenance: asyncio.Task | None = None
        self.counters: dict[str, int] = {
            "exact_hits": 0, "semantic_hits": 0, "coalesced": 0, "misses": 0,
            "redis_errors": 0, "embed_errors": 0, "stored": 0, "not_stored": 0, "guard_rejections": 0,
        }

    @property
    def semantic_enabled(self) -> bool:
        return bool(self._settings.semantic_cache and self._embedder is not None)

    # ── lookup ───────────────────────────────────────────────────────────

    async def lookup(self, fp: Fingerprint, *, threshold: float | None = None) -> CacheResult:
        if not fp.eligible:
            return CacheResult(hit=False, reason=f"ineligible:{fp.reason}")
        threshold = self._settings.similarity_threshold if threshold is None else threshold

        try:
            raw = await self._redis.get(KEY_PREFIX + fp.exact_key)
        except Exception as exc:  # noqa: BLE001 - Redis is an optimisation, not a dependency
            self.counters["redis_errors"] += 1
            logger.warning("cache.lookup.redis_error", reason=str(exc))
            return CacheResult(hit=False, reason="redis_error")
        if raw is not None:
            payload = _decode(raw)
            if payload is not None:
                self.counters["exact_hits"] += 1
                return CacheResult(True, payload["response"], "exact", 1.0, fp.exact_key)

        if self.semantic_enabled and fp.semantic_text:
            result = await self._semantic_lookup(fp, threshold)
            if result.hit:
                self.counters["semantic_hits"] += 1
                return result
            self.counters["misses"] += 1
            return result

        self.counters["misses"] += 1
        return CacheResult(hit=False, reason="no_exact_match")

    async def _semantic_lookup(self, fp: Fingerprint, threshold: float) -> CacheResult:
        text = fp.semantic_text[:MAX_EMBED_CHARS]  # type: ignore[index]
        try:
            vector = await asyncio.to_thread(self._embedder.embed, text)
        except Exception as exc:  # noqa: BLE001
            self.counters["embed_errors"] += 1
            logger.warning("cache.lookup.embed_error", reason=str(exc))
            return CacheResult(hit=False, reason="embed_error")

        self._pending_vectors[fp.exact_key] = vector
        while len(self._pending_vectors) > 256:
            self._pending_vectors.popitem(last=False)

        candidates = self._index.search(fp.partition, vector, k=5)
        if not candidates:
            return CacheResult(hit=False, reason="no_candidates_in_partition")

        best = candidates[0][1]
        reason = "below_threshold"
        for key, score in candidates:
            if score < threshold:
                break
            try:
                raw = await self._redis.get(KEY_PREFIX + key)
            except Exception as exc:  # noqa: BLE001
                self.counters["redis_errors"] += 1
                logger.warning("cache.lookup.redis_error", reason=str(exc))
                return CacheResult(hit=False, reason="redis_error")
            payload = _decode(raw) if raw is not None else None
            if payload is None:
                self._index.remove(key)  # payload expired or was evicted; drop the dangling vector
                reason = "payload_missing"
                continue
            guard = disagreement(payload.get("prompt", ""), fp.semantic_text)  # type: ignore[arg-type]
            if guard:
                self.counters["guard_rejections"] += 1
                logger.info("cache.semantic.guard_rejected", guard=guard, similarity=round(score, 4))
                reason = f"guard:{guard}"
                continue
            return CacheResult(True, payload["response"], "semantic", score, key)
        return CacheResult(hit=False, similarity_score=best, reason=reason)

    # ── store ────────────────────────────────────────────────────────────

    async def store(self, fp: Fingerprint, response: dict) -> bool:
        """Store a response if both the request and the response are cacheable. Never raises."""
        if not fp.eligible:
            return False
        ok, why = response_is_cacheable(response)
        if not ok:
            self.counters["not_stored"] += 1
            logger.debug("cache.not_stored", reason=why)
            return False

        ttl = self._settings.cache_ttl_seconds
        payload = json.dumps({
            "response": response, "prompt": fp.semantic_text or "", "model": fp.model, "created": time.time(),
        })
        try:
            await self._redis.set(KEY_PREFIX + fp.exact_key, payload, ex=ttl)
        except Exception as exc:  # noqa: BLE001
            self.counters["redis_errors"] += 1
            logger.warning("cache.store.redis_error", reason=str(exc))
            return False  # no payload, so do not index a vector that could never be served

        if self.semantic_enabled and fp.semantic_text:
            try:
                vector = self._pending_vectors.pop(fp.exact_key, None)
                if vector is None:
                    vector = await asyncio.to_thread(self._embedder.embed, fp.semantic_text[:MAX_EMBED_CHARS])
                self._index.add(fp.exact_key, fp.partition, vector, time.time() + ttl)
            except Exception as exc:  # noqa: BLE001 - exact tier still works without the vector
                self.counters["embed_errors"] += 1
                logger.warning("cache.store.embed_error", reason=str(exc))
        self.counters["stored"] += 1
        return True

    # ── single-flight ────────────────────────────────────────────────────

    def begin_flight(self, key: str) -> Flight:
        existing = self._flights.get(key)
        if existing is not None:
            self.counters["coalesced"] += 1
            return Flight(self, key, existing, leader=False)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark exceptions "retrieved"
        self._flights[key] = future
        return Flight(self, key, future, leader=True)

    # ── admin ────────────────────────────────────────────────────────────

    async def invalidate(self, key: str) -> bool:
        deleted = 0
        try:
            deleted = await self._redis.delete(KEY_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            self.counters["redis_errors"] += 1
            logger.warning("cache.invalidate.redis_error", reason=str(exc))
        removed = self._index.remove(key)
        return bool(deleted) or removed

    async def flush(self) -> dict[str, int]:
        vectors = self._index.flush()
        keys = 0
        try:
            cursor = 0
            while True:
                cursor, batch = await self._redis.scan(cursor=cursor, match=KEY_PREFIX + "*", count=500)
                if batch:
                    keys += len(batch)
                    await self._redis.delete(*batch)
                if cursor == 0:
                    break
        except Exception as exc:  # noqa: BLE001
            self.counters["redis_errors"] += 1
            logger.warning("cache.flush.redis_error", reason=str(exc))
        await self.persist()
        return {"vectors_cleared": vectors, "redis_keys_cleared": keys}

    async def stats(self) -> dict:
        keys = 0
        try:
            cursor = 0
            while True:
                cursor, batch = await self._redis.scan(cursor=cursor, match=KEY_PREFIX + "*", count=500)
                keys += len(batch)
                if cursor == 0:
                    break
        except Exception:  # noqa: BLE001
            logger.warning("cache.stats.redis_unavailable")
        return {
            "total_vectors": len(self._index),
            "redis_keys": keys,
            "semantic_enabled": self.semantic_enabled,
            "counters": dict(self.counters),
        }

    # ── lifecycle ────────────────────────────────────────────────────────

    async def persist(self) -> None:
        if not self.semantic_enabled:
            return
        path = self._settings.vector_index_path
        try:
            await asyncio.to_thread(self._index.save, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache.persist_failed", path=path, reason=str(exc))

    def start_maintenance(self) -> None:
        if self.semantic_enabled and self._maintenance is None:
            self._maintenance = asyncio.create_task(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        interval = max(5, self._settings.cache_persist_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            expired = self._index.remove_expired()
            if expired:
                logger.debug("cache.expired_vectors_removed", count=expired)
            if self._index.dirty:
                await self.persist()

    async def close(self) -> None:
        if self._maintenance is not None:
            self._maintenance.cancel()
            try:
                await self._maintenance
            except asyncio.CancelledError:
                pass
        await self.persist()
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001
            pass


def _decode(raw: str) -> dict | None:
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) and "response" in payload else None
    except (ValueError, TypeError):
        return None
