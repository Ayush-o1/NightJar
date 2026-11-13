"""Shared test fixtures for NightJar test suite."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import telemetry as tel
from app.cache import CacheResult, SemanticCache
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
        enable_cache=False,   # never touch Redis in unit tests
    )


@pytest.fixture
def mock_proxy_client() -> AsyncMock:
    """Return a mocked ProxyClient."""
    client = AsyncMock(spec=ProxyClient)
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_cache() -> AsyncMock:
    """Return a mocked SemanticCache."""
    cache = AsyncMock(spec=SemanticCache)
    cache.close = AsyncMock()
    cache.lookup.return_value = CacheResult(hit=False)
    cache.store.return_value = "mock_cache_key"
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
