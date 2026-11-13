"""Shared test fixtures for NightJar test suite."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import telemetry as tel
from app.cache import CacheResult, ResponseCache
from app.config import Settings
from app.main import app
from app.proxy import ProxyClient
from app.router import ModelRouter, RoutingDecision, Tier

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "openai_responses"


@pytest.fixture(autouse=True, scope="session")
def _init_telemetry_db() -> None:
    """Ensure the telemetry/request_log tables exist before any test runs.

    In production this happens once in app.main's lifespan before the app
    serves its first request. Tests using `test_client` skip lifespan
    entirely (deps are mocked directly onto app.state instead), so any
    test that exercises a real telemetry.py read path — e.g. GET
    /v1/telemetry — would otherwise depend on test order happening to run
    test_telemetry.py (which calls init_db() itself) first. That's an
    accidental dependency, not a designed one: a fresh checkout with no
    such ordering luck hits `sqlite3.OperationalError: no such table`.
    """
    tel.init_db()


@pytest.fixture
def fixture_dir() -> Path:
    """Return the path to the fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def chat_completion_fixture() -> dict:
    """Load the non-streaming chat completion fixture."""
    return json.loads((FIXTURES_DIR / "chat_completion.json").read_text())


@pytest.fixture
def stream_chunks_fixture() -> list[dict]:
    """Load the streaming chat completion chunks fixture."""
    return json.loads((FIXTURES_DIR / "chat_completion_stream.json").read_text())


@pytest.fixture
def error_429_fixture() -> dict:
    """Load the 429 rate limit error fixture."""
    return json.loads((FIXTURES_DIR / "error_429.json").read_text())


@pytest.fixture
def test_settings() -> Settings:
    """Return test settings with dummy keys."""
    return Settings(
        openai_api_key="sk-test-key-123",
        anthropic_api_key="sk-ant-test-123",
        log_level="DEBUG",
        similarity_threshold=0.92,
        cache_ttl_seconds=86400,
        preferred_provider="openai",
    )


@pytest.fixture
def mock_proxy_client() -> AsyncMock:
    """Return a mocked ProxyClient."""
    client = AsyncMock(spec=ProxyClient)
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_cache() -> AsyncMock:
    """Return a mocked ResponseCache (every request is the single-flight leader)."""
    cache = AsyncMock(spec=ResponseCache)
    cache.close = AsyncMock()
    cache.lookup.return_value = CacheResult(hit=False)
    cache.store.return_value = True
    cache.begin_flight = MagicMock(return_value=MagicMock(leader=True))
    return cache


@pytest.fixture
def mock_router() -> MagicMock:
    """Return a mocked ModelRouter with a default routing decision."""
    router = MagicMock(spec=ModelRouter)
    router.route.return_value = RoutingDecision(
        tier=Tier.SIMPLE,
        model_requested="gpt-3.5-turbo",
        model_selected="gpt-3.5-turbo",
        reason="token_count:2<=200",
        token_count=2,
    )
    return router


@pytest.fixture
def mock_embedder() -> MagicMock:
    """Return a mocked Embedder with deterministic outputs."""
    embedder = MagicMock()
    embedder.dimension = 384
    embedder.embed.return_value = np.random.randn(384).astype(np.float32)
    embedder.content_hash.return_value = "test_hash_abc123"
    embedder.messages_to_text.return_value = "user: Hello!"
    return embedder


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return a mocked async Redis client."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.close = AsyncMock()
    return redis


@pytest.fixture
def test_client(
    mock_proxy_client: AsyncMock, mock_cache: AsyncMock, mock_router: MagicMock, test_settings: Settings
) -> TestClient:
    """Return a FastAPI TestClient with mocked proxy, cache, and router."""
    app.state.proxy_client = mock_proxy_client
    app.state.settings = test_settings
    app.state.cache = mock_cache
    app.state.router = mock_router

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def sample_request_body() -> dict:
    """A minimal valid chat completion request body."""
    return {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Hello!"}],
    }


@pytest.fixture
def sample_streaming_request_body() -> dict:
    """A minimal valid streaming chat completion request body."""
    return {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": True,
    }


# ── Cache test infrastructure ───────────────────────────────────────────────
import hashlib  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402


class FakeEmbedder:
    """Deterministic embedder. ``set(text, vec)`` pins a vector; otherwise a text-hash vector is used.

    Real MiniLM similarities are measured in benchmarks/cache_eval.py, not in unit tests, so tests
    can state exactly how similar two prompts are.
    """

    dimension = 16

    def __init__(self) -> None:
        self.pinned: dict[str, np.ndarray] = {}
        self.calls = 0
        self.fail = False

    def set(self, text: str, vec: np.ndarray) -> None:
        self.pinned[text] = np.asarray(vec, dtype=np.float32) / np.linalg.norm(vec)

    def embed(self, text: str) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedder down")
        if text in self.pinned:
            return self.pinned[text]
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
        v = np.random.default_rng(seed).standard_normal(self.dimension).astype(np.float32)
        return v / np.linalg.norm(v)

    @staticmethod
    def pair(similarity: float) -> tuple[np.ndarray, np.ndarray]:
        """Two unit vectors with the given cosine similarity."""
        a = np.zeros(FakeEmbedder.dimension, dtype=np.float32)
        a[0] = 1.0
        b = np.zeros_like(a)
        b[0], b[1] = similarity, (1 - similarity**2) ** 0.5
        return a, b


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture(scope="session")
def redis_url():
    """A real Redis: REDIS_TEST_URL if set (CI service container), else a throwaway local server."""
    url = os.environ.get("REDIS_TEST_URL")
    if url:
        yield url
        return
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("no Redis available (set REDIS_TEST_URL or install redis-server)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
async def redis_client(redis_url):
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()
