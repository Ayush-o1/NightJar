"""Semantic cache orchestrator.

Coordinates FAISS vector similarity search with Redis key-value storage
to provide semantic caching for LLM responses.

Flow:
  1. Embed incoming prompt → vector
  2. Search FAISS for similar vectors above threshold
  3. On hit: fetch cached response from Redis → return
  4. On miss: forward to upstream, store response in Redis + vector in FAISS
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis

from app.config import Settings
from app.embedder import Embedder
from app.vector_store import VectorStore

logger = structlog.get_logger()


@dataclass
class CacheResult:
    """Result of a cache lookup."""

    hit: bool
    response: dict | None = None
    similarity_score: float = 0.0
    cache_key: str = ""


class SemanticCache:
    """Orchestrates semantic cache lookups and stores."""

    def __init__(
        self,
        embedder: Embedder,
        vector_store: VectorStore,
        redis: Redis,
        settings: Settings,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._redis = redis
        self._settings = settings

    async def lookup(
        self, model: str, messages: list[dict], *, threshold: float | None = None
    ) -> CacheResult:
        """Look up a semantically similar cached response.

        Args:
            model: The model name for context.
            messages: The conversation messages.
            threshold: Optional override for the similarity threshold.
                       Falls back to ``settings.similarity_threshold``.

        Steps:
          1. Embed the message content
          2. Search FAISS for nearest neighbor
          3. If similarity >= threshold, fetch from Redis
          4. Return CacheResult with hit/miss info
        """
        effective_threshold = (
            threshold if threshold is not None else self._settings.similarity_threshold
        )

        text = self._embedder.messages_to_text(messages)
        # Embedding is CPU-bound; keep it off the event loop so other requests keep moving.
        vector = await asyncio.to_thread(self._embedder.embed, text)

        # Search for nearest neighbor
        results = self._vector_store.search(vector, k=1)

        if not results:
            logger.debug("cache.miss", reason="empty_index")
            return CacheResult(hit=False)

        cache_key, similarity = results[0]

        if similarity < effective_threshold:
            logger.debug("cache.miss", reason="below_threshold", similarity=similarity)
            return CacheResult(hit=False, similarity_score=similarity)

        # Fetch from Redis. Redis is an optimization, not a hard dependency —
        # if it's unreachable we degrade to a cache miss instead of failing
        # the whole request (the upstream call still succeeds).
        try:
            cached_data = await self._redis.get(f"cache:{cache_key}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache.lookup.redis_error", cache_key=cache_key, reason=str(exc))
            return CacheResult(hit=False, similarity_score=similarity)

        if cached_data is None:
            # Vector exists in FAISS but Redis entry expired (TTL)
            logger.debug("cache.miss", reason="redis_expired", cache_key=cache_key)
            return CacheResult(hit=False, similarity_score=similarity)

        response = json.loads(cached_data)
        logger.info("cache.hit", similarity=similarity, cache_key=cache_key)
        return CacheResult(
            hit=True,
            response=response,
            similarity_score=similarity,
            cache_key=cache_key,
        )

    async def store(self, model: str, messages: list[dict], response: dict) -> str:
        """Store a response in the semantic cache.

        Steps:
          1. Generate content hash as cache key
          2. Embed the message content
          3. Store response JSON in Redis with TTL
          4. Add vector to FAISS index
        """
        cache_key = self._embedder.content_hash(model, messages)
        text = self._embedder.messages_to_text(messages)
        # Embedding is CPU-bound; keep it off the event loop so other requests keep moving.
        vector = await asyncio.to_thread(self._embedder.embed, text)

        # Store in Redis with TTL. Best-effort: if Redis is unreachable, still
        # index the vector — a later lookup will find it in FAISS, miss on the
        # Redis fetch, and correctly fall back to "redis_expired" (cache miss)
        # rather than losing the request's cost/latency savings entirely.
        try:
            await self._redis.set(
                f"cache:{cache_key}",
                json.dumps(response),
                ex=self._settings.cache_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache.store.redis_error", cache_key=cache_key, reason=str(exc))

        # Add to FAISS index
        self._vector_store.add(vector, cache_key)

        logger.info("cache.stored", cache_key=cache_key)
        return cache_key

    async def invalidate(self, key: str) -> bool:
        """Invalidate a specific cached entry by its Redis key.

        Removes the Redis entry and the corresponding FAISS vector.
        Returns True if the key existed and was removed.
        """
        redis_key = f"cache:{key}"
        deleted = 0
        try:
            deleted = await self._redis.delete(redis_key)
        except Exception:
            logger.warning("cache.invalidate.redis_error", cache_key=key)
        vector_removed = self._vector_store.remove_by_key(key)
        logger.info("cache.invalidated", cache_key=key, redis_deleted=deleted, vector_removed=vector_removed)
        return bool(deleted) or vector_removed

    async def flush(self) -> dict[str, int]:
        """Flush the entire cache — clear all FAISS vectors and Redis cache keys.

        Returns a dict with ``vectors_cleared`` and ``redis_keys_cleared``.
        """
        vectors_cleared = self._vector_store.flush()

        # Delete all Redis keys with the cache: prefix
        redis_keys_cleared = 0
        try:
            cursor = 0
            while True:
                cursor, keys = await self._redis.scan(cursor=cursor, match="cache:*", count=100)
                if keys:
                    redis_keys_cleared += len(keys)
                    await self._redis.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            logger.warning("cache.flush.redis_error")

        logger.info("cache.flushed", vectors_cleared=vectors_cleared, redis_keys_cleared=redis_keys_cleared)
        return {"vectors_cleared": vectors_cleared, "redis_keys_cleared": redis_keys_cleared}

    async def stats(self) -> dict[str, int]:
        """Return cache statistics.

        Returns a dict with ``total_vectors`` and ``redis_keys``.
        """
        total_vectors = self._vector_store.size

        redis_keys = 0
        try:
            cursor = 0
            while True:
                cursor, keys = await self._redis.scan(cursor=cursor, match="cache:*", count=100)
                redis_keys += len(keys)
                if cursor == 0:
                    break
        except Exception:
            logger.warning("cache.stats.redis_unavailable")

        return {"total_vectors": total_vectors, "redis_keys": redis_keys}

    async def close(self) -> None:
        """Persist FAISS index and close Redis connection."""
        self._vector_store.persist()
        try:
            await self._redis.close()
        except Exception:
            pass
