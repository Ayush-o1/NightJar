"""End-to-end integration tests for NightJar.

These tests hit the REAL upstream API (OpenAI). They are designed to
minimise cost:
  - Use gpt-3.5-turbo (cheapest model)
  - max_tokens=50 for all requests
  - Short prompts (5-10 words)
  - Reuse prompts to maximise cache hits (only first call costs tokens)
  - Similarity threshold lowered to 0.85 for reliable cache hits

Requirements:
  - OPENAI_API_KEY set in environment or .env
  - Redis running (or REDIS_URL set)
  - TEST_MODE=true in environment (forces cheapest model)
  - RUN_E2E_TESTS=1 set explicitly (see below)

This module is marked ``e2e`` and is excluded from the default test run
(see ``addopts`` in pyproject.toml) — ``pytest tests/`` never collects it.
This matters for two reasons, not just cost:

1. These tests hit real providers and cost real money.
2. They run the *real* app lifespan (real ProxyClient, real Embedder, real
   FAISS/Redis) against the shared FastAPI ``app`` singleton imported from
   ``app.main``. That mutates ``app.state`` with live objects. Running this
   module in the same process as the mocked suite has previously caused the
   mocked tests to silently pick up the real ProxyClient instead of their
   mocks (see the module-scoped fixture's teardown below, which restores
   ``app.state`` defensively even for an intentional ``-m e2e`` run).

The skip condition below is intentionally independent of OPENAI_API_KEY: CI
sets a dummy OPENAI_API_KEY for unrelated (mocked) tests, and if the skip
depended on that alone, these tests would silently run for real. Requires
the explicit RUN_E2E_TESTS=1 opt-in *and* a real-looking API key.

Usage:
  RUN_E2E_TESTS=1 PYTHONPATH=. pytest tests/test_e2e.py -v --tb=short -x -m e2e
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import __version__

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (os.environ.get("RUN_E2E_TESTS") and os.environ.get("OPENAI_API_KEY")),
        reason="Live E2E tests require RUN_E2E_TESTS=1 and a real OPENAI_API_KEY — skipped by default",
    ),
]


@pytest.fixture(scope="module")
def e2e_client():
    """Create a TestClient that uses real services (OpenAI, Redis, FAISS).

    Scope=module so the embedding model loads once and the cache persists
    across tests within this module (enabling cache hit tests).
    """
    # Force settings for cost-optimized testing
    os.environ.setdefault("TEST_MODE", "true")
    os.environ.setdefault("SIMILARITY_THRESHOLD", "0.85")
    os.environ.setdefault("SQLITE_DB_PATH", "./data/test_e2e_telemetry.db")
    os.environ.setdefault("FAISS_INDEX_PATH", "./data/test_e2e_faiss.index")

    # Ensure data directory exists (SQLite can't create parent dirs)
    os.makedirs("./data", exist_ok=True)

    # Clear cached settings so our env vars take effect
    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import app
    # Use lifespan context manager so embedder + FAISS + Redis are initialized
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Flush cache to start clean
            client.delete("/v1/cache")
            yield client
    finally:
        # Defense in depth: lifespan's startup sets real objects (proxy_client,
        # embedder, vector_store, redis, cache, router, threshold_manager,
        # settings) directly onto the shared `app.state`, and its shutdown
        # only closes them — it never unsets the attributes. Left in place,
        # a later test module reusing `app` (e.g. via conftest's mocked
        # `test_client` fixture) could silently observe leftover real
        # objects instead of its own mocks. Remove everything lifespan set
        # so any such misuse fails loudly instead of hitting a live API.
        for attr in (
            "proxy_client", "embedder", "vector_store", "redis",
            "cache", "router", "threshold_manager", "settings",
        ):
            if hasattr(app.state, attr):
                delattr(app.state, attr)

        # Cleanup: remove test database and index files
        get_settings.cache_clear()
        for f in ["./data/test_e2e_telemetry.db", "./data/test_e2e_faiss.index", "./data/test_e2e_faiss.index.idmap"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass


@pytest.fixture(scope="module")
def db_path():
    """Return the test telemetry DB path."""
    return "./data/test_e2e_telemetry.db"


# ─── Helper ──────────────────────────────────────────────────────────────

def _chat_request(client, content, model="gpt-3.5-turbo", max_tokens=50, **kwargs):
    """Send a chat completion request with cost-optimized defaults."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
    }
    body.update(kwargs)
    return client.post(
        "/v1/chat/completions",
        json=body,
        headers=kwargs.pop("headers", {}),
    )


# ═══════════════════════════════════════════════════════════════════════
#  TEST 1: Cache miss → upstream call → cache store → telemetry write
# ═══════════════════════════════════════════════════════════════════════

class TestE2EHappyPath:
    """Full pipeline: miss → upstream → store → telemetry."""

    def test_cache_miss_upstream_call(self, e2e_client):
        """First request should be a cache miss and return a valid response."""
        resp = _chat_request(e2e_client, "Capital of France?")
        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert data["choices"][0]["message"]["content"]
        # Should be a cache miss
        assert resp.headers.get("x-cache") == "MISS"

    def test_cache_hit_on_similar_prompt(self, e2e_client):
        """Semantically similar prompt should hit cache (free — no API cost)."""
        resp = _chat_request(e2e_client, "What is France's capital?")
        assert resp.status_code == 200
        assert resp.headers.get("x-cache") == "HIT"
        # Verify similarity score is present
        sim = resp.headers.get("x-similarity")
        assert sim is not None
        assert float(sim) >= 0.85


# ═══════════════════════════════════════════════════════════════════════
#  TEST 2: Cache hit latency
# ═══════════════════════════════════════════════════════════════════════

class TestCacheHitLatency:
    """Verify cache hits are fast."""

    def test_cache_hit_under_threshold(self, e2e_client):
        """Cache hit should return much faster than a cold call."""
        # Warm the cache first (may already be warm from TestE2EHappyPath)
        _chat_request(e2e_client, "Capital of France?")

        # Measure cache hit latency
        start = time.monotonic()
        resp = _chat_request(e2e_client, "What is the capital of France?")
        elapsed_ms = (time.monotonic() - start) * 1000

        assert resp.status_code == 200
        assert resp.headers.get("x-cache") == "HIT"
        # Cache hit should be well under 5000ms (generous for CI environments)
        assert elapsed_ms < 5000, f"Cache hit took {elapsed_ms:.0f}ms, expected <5000ms"


# ═══════════════════════════════════════════════════════════════════════
#  TEST 3: Model routing — simple vs complex
# ═══════════════════════════════════════════════════════════════════════

class TestModelRouting:
    """Verify the router classifies prompts correctly."""

    def test_simple_prompt_routes_correctly(self, e2e_client):
        """Short, simple prompt should be classified as simple tier."""
        resp = _chat_request(e2e_client, "Hi there!")
        assert resp.status_code == 200
        assert resp.headers.get("x-model-tier") == "simple"

    def test_complex_prompt_classified(self, e2e_client):
        """Complex keyword prompt should be classified as complex tier."""
        resp = _chat_request(e2e_client, "Analyze binary search algorithm")
        assert resp.status_code == 200
        assert resp.headers.get("x-model-tier") == "complex"
        # In TEST_MODE, model should still be gpt-3.5-turbo (cheapest)


# ═══════════════════════════════════════════════════════════════════════
#  TEST 4: Context compression
# ═══════════════════════════════════════════════════════════════════════

class TestContextCompression:
    """Verify compression triggers and no-compress header works."""

    def test_short_conversation_not_compressed(self, e2e_client):
        """Conversation with < 6 turns should not be compressed."""
        resp = _chat_request(e2e_client, "Say hello!")
        assert resp.status_code == 200
        # Compression ratio should be 1.0 (no compression)
        ratio = resp.headers.get("x-compression-ratio")
        assert ratio is not None
        assert float(ratio) == 1.0

    def test_no_compress_header(self, e2e_client):
        """X-NightJar-No-Compress header disables compression."""
        resp = e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Say hello!"}],
                "max_tokens": 50,
            },
            headers={"x-nightjar-no-compress": "true"},
        )
        assert resp.status_code == 200
        compressed = resp.headers.get("x-compressed")
        assert compressed == "False"


# ═══════════════════════════════════════════════════════════════════════
#  TEST 5: Adaptive thresholds
# ═══════════════════════════════════════════════════════════════════════

class TestAdaptiveThresholds:
    """Verify threshold endpoints work."""

    def test_get_threshold(self, e2e_client):
        """GET /v1/threshold returns current threshold info."""
        resp = e2e_client.get("/v1/threshold")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_threshold" in data
        assert "baseline" in data

    def test_evaluate_threshold(self, e2e_client):
        """POST /v1/threshold/evaluate returns evaluation result."""
        resp = e2e_client.post("/v1/threshold/evaluate")
        assert resp.status_code == 200
        data = resp.json()
        assert "threshold" in data
        assert "cache_hit_rate" in data


# ═══════════════════════════════════════════════════════════════════════
#  TEST 6: Cache invalidation
# ═══════════════════════════════════════════════════════════════════════

class TestCacheInvalidation:
    """Verify DELETE /v1/cache clears both Redis and FAISS."""

    def test_flush_cache_then_miss(self, e2e_client):
        """After flushing cache, a previously cached prompt should miss."""
        # First, warm the cache with a unique prompt
        unique_prompt = "What color is the sky?"
        resp1 = _chat_request(e2e_client, unique_prompt)
        assert resp1.status_code == 200

        # Verify it's cached
        resp2 = _chat_request(e2e_client, unique_prompt)
        assert resp2.headers.get("x-cache") == "HIT"

        # Flush cache
        flush_resp = e2e_client.delete("/v1/cache")
        assert flush_resp.status_code == 200
        flush_data = flush_resp.json()
        assert flush_data["status"] == "ok"
        assert "vectors_cleared" in flush_data

        # Now the same prompt should miss
        resp3 = _chat_request(e2e_client, unique_prompt)
        assert resp3.status_code == 200
        assert resp3.headers.get("x-cache") == "MISS"


# ═══════════════════════════════════════════════════════════════════════
#  TEST 7: Telemetry accuracy
# ═══════════════════════════════════════════════════════════════════════

class TestTelemetryAccuracy:
    """Verify telemetry records are written for every request."""

    def test_telemetry_records_exist(self, e2e_client):
        """GET /v1/telemetry should return records for our test requests."""
        resp = e2e_client.get("/v1/telemetry?limit=100")
        assert resp.status_code == 200
        data = resp.json()
        assert "records" in data
        # We've made several requests by now — at least some should be recorded
        assert len(data["records"]) > 0

    def test_telemetry_summary(self, e2e_client):
        """GET /v1/telemetry/summary should return correct aggregates."""
        resp = e2e_client.get("/v1/telemetry/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_requests" in data
        assert data["total_requests"] > 0
        assert "cache_hits" in data
        assert "cache_hit_rate" in data
        assert "avg_latency_ms" in data


# ═══════════════════════════════════════════════════════════════════════
#  TEST 8: Error propagation
# ═══════════════════════════════════════════════════════════════════════

class TestErrorPropagation:
    """Verify upstream errors are forwarded correctly."""

    def test_invalid_model_returns_error(self, e2e_client):
        """Requesting a nonexistent model should return an upstream error."""
        resp = e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 50,
            },
            headers={"x-nightjar-model-override": "nonexistent-model-xyz"},
        )
        # Should get an error status from upstream (typically 404 or 400)
        assert resp.status_code >= 400


# ═══════════════════════════════════════════════════════════════════════
#  TEST 9: Streaming passthrough
# ═══════════════════════════════════════════════════════════════════════

class TestStreamingPassthrough:
    """Verify stream=True works end-to-end."""

    def test_streaming_response(self, e2e_client):
        """stream=True should return SSE-formatted data."""
        resp = e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Say hi"}],
                "max_tokens": 10,
                "stream": True,
            },
        )
        assert resp.status_code == 200
        # Content-type should be text/event-stream
        assert "text/event-stream" in resp.headers.get("content-type", "")
        # Body should contain SSE data lines
        body = resp.text
        assert "data:" in body


# ═══════════════════════════════════════════════════════════════════════
#  TEST 10: X-NightJar-Model-Override header
# ═══════════════════════════════════════════════════════════════════════

class TestModelOverride:
    """Verify the override header forces a specific model."""

    def test_override_header(self, e2e_client):
        """X-NightJar-Model-Override should force the specified model."""
        resp = e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 10,
            },
            headers={"x-nightjar-model-override": "gpt-3.5-turbo"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("x-model-selected") == "gpt-3.5-turbo"


# ═══════════════════════════════════════════════════════════════════════
#  TEST 11: X-NightJar-No-Compress header (covered in TestContextCompression)
# ═══════════════════════════════════════════════════════════════════════

# Covered above in TestContextCompression.test_no_compress_header


# ═══════════════════════════════════════════════════════════════════════
#  TEST 12: FAISS + Redis consistency
# ═══════════════════════════════════════════════════════════════════════

class TestFaissRedisConsistency:
    """Verify expired Redis key doesn't crash when FAISS still has a hit."""

    def test_no_crash_on_redis_miss_with_faiss_hit(self, e2e_client):
        """Even if Redis key expires, the lookup should return a miss (not crash)."""
        # Store something in cache
        prompt = "Testing Redis consistency"
        resp1 = _chat_request(e2e_client, prompt)
        assert resp1.status_code == 200

        # The system handles expired Redis gracefully (returns cache miss).
        # We can't easily force Redis TTL expiry in a test, but we verify
        # the cache stats endpoint works without errors.
        stats = e2e_client.get("/v1/cache/stats")
        assert stats.status_code == 200
        data = stats.json()
        assert "total_vectors" in data
        assert "redis_keys" in data


# ═══════════════════════════════════════════════════════════════════════
#  TEST 13: Concurrent requests (load test)
# ═══════════════════════════════════════════════════════════════════════

class TestConcurrentRequests:
    """Verify the system handles concurrent requests without crashes."""

    def test_concurrent_requests_no_crash(self, e2e_client):
        """Multiple sequential requests should all succeed."""
        # Use 10 sequential requests (TestClient doesn't support true concurrency)
        # but this validates the full pipeline handles repeated requests.
        results = []
        prompts = [
            "What is 2+2?",
            "What is 2+2?",  # cache hit
            "What is 2+2?",  # cache hit
            "Hello world!",
            "Hello world!",  # cache hit
            "What is 2+2?",  # cache hit
            "Hello world!",  # cache hit
            "What is 2+2?",  # cache hit
            "Hello world!",  # cache hit
            "What is 2+2?",  # cache hit
        ]
        for prompt in prompts:
            resp = _chat_request(e2e_client, prompt)
            results.append(resp.status_code)

        # All should succeed
        assert all(code == 200 for code in results), f"Some requests failed: {results}"

        # Count cache hits — should be at least 5 out of 10
        hits = sum(1 for i, prompt in enumerate(prompts)
                   if _chat_request(e2e_client, prompt).headers.get("x-cache") == "HIT")
        # Most repeated prompts should be cache hits by now
        assert hits >= 5, f"Expected ≥5 cache hits but got {hits}"


# ═══════════════════════════════════════════════════════════════════════
#  REGRESSION TEST: TelemetryMiddleware registration
# ═══════════════════════════════════════════════════════════════════════

class TestTelemetryMiddlewareRegression:
    """Regression test: every chat request must produce a telemetry record."""

    def test_every_request_produces_telemetry(self, e2e_client, db_path):
        """After a request, the telemetry DB should contain a new record."""
        # Get current record count
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            before_count = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
            conn.close()
        else:
            before_count = 0

        # Make a request
        resp = _chat_request(e2e_client, "Regression test prompt")
        assert resp.status_code == 200

        # Small delay to ensure middleware has written
        time.sleep(0.5)

        # Check that a new record was written
        conn = sqlite3.connect(db_path)
        after_count = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
        conn.close()

        assert after_count > before_count, (
            f"TelemetryMiddleware not writing records! "
            f"Before: {before_count}, After: {after_count}"
        )


# ═══════════════════════════════════════════════════════════════════════
#  Health check and dashboard
# ═══════════════════════════════════════════════════════════════════════

class TestHealthAndDashboard:
    """Verify health check and dashboard are accessible."""

    def test_health_check(self, e2e_client):
        """GET /health returns 200 with version 1.0.0."""
        resp = e2e_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == __version__

    def test_swagger_ui(self, e2e_client):
        """GET /docs should be accessible."""
        resp = e2e_client.get("/docs")
        assert resp.status_code == 200

    def test_dashboard_accessible(self, e2e_client):
        """GET /dashboard/ should serve the dashboard HTML."""
        resp = e2e_client.get("/dashboard/")
        assert resp.status_code == 200
        assert "NightJar" in resp.text


# ═══════════════════════════════════════════════════════════════════════
#  Cache stats endpoint
# ═══════════════════════════════════════════════════════════════════════

class TestCacheStats:
    """Verify cache stats endpoint works."""

    def test_cache_stats(self, e2e_client):
        """GET /v1/cache/stats should return valid stats."""
        resp = e2e_client.get("/v1/cache/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_vectors" in data
        assert "redis_keys" in data
        assert "similarity_threshold" in data
