"""Tests for the adaptive similarity threshold system.

All tests use temporary SQLite databases — no live services required.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from app.adaptive import ThresholdManager
from app.config import Settings

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_settings(**overrides) -> Settings:
    defaults = {
        "openai_api_key": "test",
        "similarity_threshold": 0.92,
        "adaptive_threshold_enabled": True,
        "adaptive_threshold_window": 100,
        "adaptive_threshold_min": 0.70,
        "adaptive_threshold_max": 0.98,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _seed_telemetry(db_path: str, total: int, hits: int) -> None:
    """Insert fake telemetry rows with the given hit/miss ratio."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            timestamp TEXT,
            model_requested TEXT,
            model_used TEXT,
            cache_hit BOOLEAN,
            similarity_score REAL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            estimated_cost_usd REAL,
            latency_ms REAL,
            compressed BOOLEAN,
            compression_ratio REAL
        )
    """)
    for i in range(total):
        is_hit = i < hits
        conn.execute(
            "INSERT INTO telemetry (request_id, timestamp, cache_hit) VALUES (?, ?, ?)",
            (f"req-{i}", "2025-01-01T00:00:00", is_hit),
        )
    conn.commit()
    conn.close()


# ── Unit Tests ───────────────────────────────────────────────────────────


class TestAdaptiveThreshold:
    """Tests for ThresholdManager evaluation logic."""

    @pytest.fixture
    def db_path(self, tmp_path) -> str:
        return str(tmp_path / "test_adaptive.db")

    def test_threshold_raises_when_hit_rate_high(self, db_path):
        """Cache hit rate > 60% → threshold increases by 0.01."""
        _seed_telemetry(db_path, total=100, hits=70)  # 70% hit rate
        settings = _make_settings(similarity_threshold=0.92)
        manager = ThresholdManager(db_path)
        result = manager.evaluate(settings)
        assert result["threshold"] == pytest.approx(0.93, abs=1e-4)

    def test_threshold_lowers_when_hit_rate_low(self, db_path):
        """Cache hit rate < 20% → threshold decreases by 0.01."""
        _seed_telemetry(db_path, total=100, hits=10)  # 10% hit rate
        settings = _make_settings(similarity_threshold=0.92)
        manager = ThresholdManager(db_path)
        result = manager.evaluate(settings)
        assert result["threshold"] == pytest.approx(0.91, abs=1e-4)

    def test_threshold_unchanged_in_normal_range(self, db_path):
        """Cache hit rate between 20–60% → no change."""
        _seed_telemetry(db_path, total=100, hits=40)  # 40% hit rate
        settings = _make_settings(similarity_threshold=0.92)
        manager = ThresholdManager(db_path)
        result = manager.evaluate(settings)
        assert result["threshold"] == pytest.approx(0.92, abs=1e-4)

    def test_threshold_capped_at_max(self, db_path):
        """Threshold cannot exceed the configured maximum (0.98)."""
        _seed_telemetry(db_path, total=100, hits=80)  # 80% hit rate
        settings = _make_settings(similarity_threshold=0.92, adaptive_threshold_max=0.98)
        manager = ThresholdManager(db_path)

        # Seed threshold_history at 0.98 already
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO threshold_history (threshold, cache_hit_rate, evaluated_at) VALUES (?, ?, ?)",
            (0.98, 0.80, "2025-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        result = manager.evaluate(settings)
        assert result["threshold"] <= 0.98

    def test_threshold_floored_at_min(self, db_path):
        """Threshold cannot go below the configured minimum (0.70)."""
        _seed_telemetry(db_path, total=100, hits=5)  # 5% hit rate
        settings = _make_settings(similarity_threshold=0.92, adaptive_threshold_min=0.70)
        manager = ThresholdManager(db_path)

        # Seed threshold_history at 0.70 already
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO threshold_history (threshold, cache_hit_rate, evaluated_at) VALUES (?, ?, ?)",
            (0.70, 0.05, "2025-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        result = manager.evaluate(settings)
        assert result["threshold"] >= 0.70

    def test_threshold_history_written_to_db(self, db_path):
        """Evaluation writes a record to the threshold_history table."""
        _seed_telemetry(db_path, total=50, hits=25)
        settings = _make_settings()
        manager = ThresholdManager(db_path)

        manager.evaluate(settings)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM threshold_history").fetchall()
        conn.close()

        assert len(rows) == 1
        row = dict(rows[0])
        assert "threshold" in row
        assert "cache_hit_rate" in row
        assert "evaluated_at" in row


# ── Endpoint Tests ───────────────────────────────────────────────────────


class TestThresholdEndpoints:
    """Tests for the GET /v1/threshold and POST /v1/threshold/evaluate endpoints."""

    @pytest.fixture
    def endpoint_client(self, tmp_path):
        """Create a TestClient with a ThresholdManager backed by a tmp DB."""
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient

        from app.cache import CacheResult
        from app.main import app
        from app.router import RoutingDecision, Tier

        db_path = str(tmp_path / "endpoint_test.db")
        _seed_telemetry(db_path, total=50, hits=25)

        settings = _make_settings(sqlite_db_path=db_path)
        manager = ThresholdManager(db_path)

        # Mock other app state dependencies
        mock_proxy = AsyncMock()
        mock_proxy.close = AsyncMock()
        mock_cache = AsyncMock()
        mock_cache.close = AsyncMock()
        mock_cache.lookup.return_value = CacheResult(hit=False)
        mock_router = MagicMock()
        mock_router.route.return_value = RoutingDecision(
            tier=Tier.SIMPLE, model_requested="gpt-3.5-turbo",
            model_selected="gpt-3.5-turbo", reason="test", token_count=2,
        )

        app.state.settings = settings
        app.state.threshold_manager = manager
        app.state.proxy_client = mock_proxy
        app.state.cache = mock_cache
        app.state.router = mock_router

        return TestClient(app, raise_server_exceptions=False)

    def test_get_threshold_endpoint(self, endpoint_client):
        """GET /v1/threshold returns correct schema."""
        resp = endpoint_client.get("/v1/threshold")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_threshold" in data
        assert "baseline" in data
        assert "last_evaluated_at" in data
        assert isinstance(data["current_threshold"], float)
        assert isinstance(data["baseline"], float)

    def test_post_evaluate_endpoint(self, endpoint_client):
        """POST /v1/threshold/evaluate triggers evaluation and returns new value."""
        resp = endpoint_client.post("/v1/threshold/evaluate")
        assert resp.status_code == 200
        data = resp.json()
        assert "threshold" in data
        assert "cache_hit_rate" in data
        assert "evaluated_at" in data
        assert isinstance(data["threshold"], float)

    def test_history_endpoint_empty_before_any_evaluation(self, endpoint_client):
        """History starts empty rather than erroring — the dashboard renders
        an empty state from this."""
        resp = endpoint_client.get("/v1/threshold/history")
        assert resp.status_code == 200
        assert resp.json()["records"] == []

    def test_history_endpoint_returns_evaluations_newest_first(self, endpoint_client):
        """Each evaluation is recorded and returned newest-first."""
        endpoint_client.post("/v1/threshold/evaluate")
        endpoint_client.post("/v1/threshold/evaluate")

        resp = endpoint_client.get("/v1/threshold/history")
        assert resp.status_code == 200
        records = resp.json()["records"]

        assert len(records) == 2
        assert set(records[0]) == {"threshold", "cache_hit_rate", "evaluated_at"}
        assert records[0]["evaluated_at"] >= records[1]["evaluated_at"]

    def test_history_endpoint_respects_limit(self, endpoint_client):
        for _ in range(3):
            endpoint_client.post("/v1/threshold/evaluate")

        resp = endpoint_client.get("/v1/threshold/history?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()["records"]) == 2

    def test_history_endpoint_rejects_bad_limit(self, endpoint_client):
        assert endpoint_client.get("/v1/threshold/history?limit=0").status_code == 400
        assert endpoint_client.get("/v1/threshold/history?limit=9999").status_code == 400
