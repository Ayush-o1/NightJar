"""Schema migration, failure visibility, attribution, failed-request records, streaming usage, retention."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import telemetry as tel
from app.cache import ResponseCache
from app.config import Settings
from app.keys import KeyStore
from app.limits import ConcurrencyLimiter
from app.main import _usage_from_chunk, app
from app.proxy import ProxyClient
from app.ratelimit import RateLimiter
from app.router import ModelRouter
from app.vector_index import VectorIndex
from tests.conftest import FakeEmbedder

BASELINE_SCHEMA = """
CREATE TABLE telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE, timestamp DATETIME, model_requested TEXT,
    model_used TEXT, tier TEXT, routing_reason TEXT, cache_hit BOOLEAN, similarity_score REAL,
    prompt_tokens INTEGER, completion_tokens INTEGER, estimated_cost_usd REAL, latency_ms REAL,
    compressed BOOLEAN, compression_ratio REAL);
"""


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(tel, "DB_PATH", path)
    return path


class TestMigration:
    def test_upgrades_a_baseline_database_in_place_without_losing_rows(self, fresh_db):
        conn = sqlite3.connect(fresh_db)
        conn.executescript(BASELINE_SCHEMA)
        conn.execute(
            "INSERT INTO telemetry (request_id, timestamp, model_used, prompt_tokens, completion_tokens,"
            " estimated_cost_usd, cache_hit) VALUES ('old-1', '2026-01-01T00:00:00+00:00', 'gpt-4o', 10, 5, 0.01, 0)"
        )
        conn.commit()
        conn.close()

        tel.init_db()

        with tel.get_conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(telemetry)")}
            assert {"api_key_name", "http_status", "error_code", "cache_tier"} <= cols
            assert c.execute("PRAGMA user_version").fetchone()[0] == tel.SCHEMA_VERSION
            row = c.execute("SELECT * FROM telemetry WHERE request_id='old-1'").fetchone()
            assert row["model_used"] == "gpt-4o" and row["prompt_tokens"] == 10 and row["api_key_name"] is None

    def test_is_idempotent_and_safe_if_a_column_already_exists(self, fresh_db):
        tel.init_db()
        tel.init_db()
        with tel.get_conn() as c:
            c.execute("PRAGMA user_version = 0")
        tel.init_db()  # re-runs the migration against a table that already has the columns
        with tel.get_conn() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == tel.SCHEMA_VERSION

    def test_old_style_record_without_new_fields_still_writes(self, fresh_db):
        tel.init_db()
        tel.write_record({"request_id": "r1", "timestamp": "2026-01-01T00:00:00+00:00", "model_requested": "m",
                          "model_used": "m", "cache_hit": False, "similarity_score": None, "prompt_tokens": 1,
                          "completion_tokens": 1, "estimated_cost_usd": 0.0, "latency_ms": 1.0, "compressed": False,
                          "compression_ratio": 1.0})
        assert tel.get_records(1)[0]["request_id"] == "r1"


class TestWriteFailuresAreVisible:
    def test_failure_is_counted_and_logged_not_swallowed(self, fresh_db, monkeypatch):
        tel.init_db()
        before = tel.write_failures["telemetry"]

        def boom():
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(tel, "get_conn", boom)
        tel.write_record({"request_id": "x"})  # must not raise
        assert tel.write_failures["telemetry"] == before + 1

    async def test_health_reports_the_count(self, test_client):
        tel.write_failures["telemetry"] += 1
        assert test_client.get("/health").json()["telemetry_write_failures"] >= 1


class TestRetention:
    def test_purges_only_old_rows(self, fresh_db):
        tel.init_db()
        now = datetime.now(timezone.utc)
        for rid, age in (("old", 100), ("recent", 5)):
            tel.write_record({"request_id": rid, "timestamp": (now - timedelta(days=age)).isoformat(),
                              "model_requested": "m", "model_used": "m", "cache_hit": False, "similarity_score": None,
                              "prompt_tokens": 1, "completion_tokens": 1, "estimated_cost_usd": 0.0, "latency_ms": 1.0,
                              "compressed": False, "compression_ratio": 1.0})
        assert tel.purge_older_than(90) == 1
        assert [r["request_id"] for r in tel.get_records(10)] == ["recent"]

    def test_zero_disables(self, fresh_db):
        tel.init_db()
        assert tel.purge_older_than(0) == 0


class TestUsageChunkParsing:
    def test_usage_only_chunk(self):
        chunk = 'data: {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}\n\n'
        usage, only = _usage_from_chunk(chunk)
        assert usage == {"prompt_tokens": 3, "completion_tokens": 4} and only

    def test_usage_riding_on_a_content_chunk_is_not_dropped(self):
        chunk = 'data: {"choices": [{"delta": {"content": "x"}}], "usage": {"prompt_tokens": 1}}\n\n'
        usage, only = _usage_from_chunk(chunk)
        assert usage and not only

    @pytest.mark.parametrize("chunk", ['data: {"choices": [{"delta": {"content": "hi"}}]}\n\n', "data: [DONE]\n\n",
                                       'data: {"choices": [], "usage": null}\n\n', "data: {broken usage\n\n"])
    def test_chunks_without_usage(self, chunk):
        assert _usage_from_chunk(chunk) == (None, False)


@pytest.fixture
def wired(provider, tmp_path, redis_client, monkeypatch):
    monkeypatch.setattr(tel, "DB_PATH", str(tmp_path / "w.db"))
    tel.init_db()
    settings = Settings(openai_api_key="sk-fake", upstream_retries=0, nightjar_auth="required",
                        sqlite_db_path=str(tmp_path / "w.db"))
    app.state.settings = settings
    app.state.proxy_client = ProxyClient(settings)
    app.state.router = ModelRouter(config_path="config/routing_rules.yaml")
    app.state.limiter = ConcurrencyLimiter(8)
    app.state.rate_limiter = RateLimiter()
    app.state.cache = ResponseCache(None, VectorIndex(FakeEmbedder.dimension), redis_client, settings)
    store = KeyStore(str(tmp_path / "w.db"))
    app.state.key_store = store
    return store


def api():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def post(c, key, body, **headers):
    return c.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}", **headers})


@pytest.mark.asyncio
class TestRequestRecords:
    async def test_record_carries_key_name_status_and_tier(self, wired):
        key = wired.create("billing-svc", {"chat"})
        body = {"model": "gpt-4o", "temperature": 0, "messages": [{"role": "user", "content": "attribution probe"}]}
        async with api() as c:
            miss = await post(c, key, {**body, "user": "spoofed-user"})
            hit = await post(c, key, body)
        rows = {r["request_id"]: r for r in tel.get_records(5)}
        assert rows[miss.headers["x-request-id"]]["api_key_name"] == "billing-svc"
        assert rows[miss.headers["x-request-id"]]["http_status"] == 200
        assert rows[hit.headers["x-request-id"]]["cache_tier"] == "exact"
        assert "spoofed-user" not in str(rows)

    async def test_failed_requests_are_recorded_with_status_and_error_code(self, wired, provider):
        key = wired.create("svc", {"chat"})
        async with api() as c:
            resp = await post(c, key, {"model": "gpt-4o", "messages": [{"role": "user", "content": "[fail:500]"}]})
        assert resp.status_code == 502
        row = tel.get_records(1)[0]
        assert row["request_id"] == resp.headers["x-request-id"]
        assert (row["http_status"], row["error_code"]) == (502, "upstream_error")
        assert tel.get_summary()["error_requests"] >= 1

    async def test_model_policy_rejection_is_recorded(self, wired):
        key = wired.create("narrow", {"chat"}, ["gpt-4o-mini"])
        async with api() as c:
            await post(c, key, {"model": "o1-pro", "messages": [{"role": "user", "content": "x"}]})
        row = tel.get_records(1)[0]
        assert (row["http_status"], row["error_code"], row["api_key_name"]) == (403, "model_not_allowed", "narrow")


@pytest.mark.asyncio
class TestStreamingUsage:
    async def stream(self, c, key, **extra):
        messages = [{"role": "user", "content": "count my tokens"}]
        body = {"model": "gpt-4o", "stream": True, "messages": messages, **extra}
        return await post(c, key, body)

    async def test_streamed_request_records_real_token_counts_and_cost(self, wired, provider):
        key = wired.create("svc", {"chat"})
        async with api() as c:
            resp = await self.stream(c, key)
        assert resp.text.rstrip().endswith("[DONE]")
        row = tel.get_records(1)[0]
        assert row["prompt_tokens"] and row["completion_tokens"] and row["estimated_cost_usd"] > 0
        assert row["http_status"] == 200

    async def test_usage_chunk_is_hidden_from_callers_who_did_not_ask(self, wired, provider):
        key = wired.create("svc", {"chat"})
        async with api() as c:
            resp = await self.stream(c, key)
        for line in resp.text.split("\n\n"):
            if line.startswith("data: {"):
                assert json.loads(line[6:]).get("choices"), "a chunk with empty choices would break naive clients"

    async def test_usage_chunk_is_passed_through_when_the_caller_asked(self, wired, provider):
        key = wired.create("svc", {"chat"})
        async with api() as c:
            resp = await self.stream(c, key, stream_options={"include_usage": True})
        chunks = [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: {")]
        assert any(not ch.get("choices") and ch.get("usage") for ch in chunks)

    async def test_stream_latency_still_covers_the_whole_stream(self, wired, provider):
        provider.config["stream_delay_ms"] = 80
        key = wired.create("svc", {"chat"})
        async with api() as c:
            await self.stream(c, key)
        assert tel.get_records(1)[0]["latency_ms"] > 300
