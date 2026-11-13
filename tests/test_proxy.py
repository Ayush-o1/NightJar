"""Integration tests for the proxy passthrough endpoint.

All tests use recorded fixtures — no live API calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from app import __version__
from app.proxy import UpstreamError

# ───────────────────────── Health Check ──────────────────────────────────


class TestHealthCheck:
    """Tests for GET /health."""

    def test_health_returns_200(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == __version__

    def test_health_reports_redis_unknown_when_not_wired(self, test_client):
        """test_client's fixture doesn't set app.state.redis at all."""
        resp = test_client.get("/health")
        assert resp.json()["redis"] == "unknown"

    def test_health_status_ok_even_when_redis_unreachable(self, test_client):
        """Redis is optional — its outage must not fail the liveness probe."""
        from app.main import app

        broken_redis = AsyncMock()
        broken_redis.ping = AsyncMock(side_effect=ConnectionError("down"))
        app.state.redis = broken_redis
        try:
            resp = test_client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert resp.json()["redis"] == "unreachable"
        finally:
            del app.state.redis

    def test_health_reports_redis_ok_when_reachable(self, test_client):
        from app.main import app

        healthy_redis = AsyncMock()
        healthy_redis.ping = AsyncMock(return_value=True)
        app.state.redis = healthy_redis
        try:
            resp = test_client.get("/health")
            assert resp.json()["redis"] == "ok"
        finally:
            del app.state.redis


# ──────────────────── Non-Streaming Completions ──────────────────────────


class TestChatCompletionsNonStreaming:
    """Tests for POST /v1/chat/completions (stream=false)."""

    def test_successful_completion(
        self,
        test_client,
        mock_proxy_client,
        chat_completion_fixture,
        sample_request_body,
    ):
        """Verify passthrough returns the upstream response as-is."""
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "chatcmpl-abc123"
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello! How can I help you today?"
        assert data["usage"]["total_tokens"] == 18

        mock_proxy_client.forward.assert_called_once()

    def test_response_preserves_all_fields(
        self,
        test_client,
        mock_proxy_client,
        chat_completion_fixture,
        sample_request_body,
    ):
        """Verify no fields are dropped during passthrough."""
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)

        data = resp.json()
        assert data["model"] == "gpt-3.5-turbo-0125"
        assert data["system_fingerprint"] == "fp_abc123"
        assert data["choices"][0]["finish_reason"] == "stop"

    def test_missing_messages_returns_400(self, test_client):
        """Verify request validation rejects missing messages field."""
        resp = test_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"

    def test_missing_model_returns_400(self, test_client):
        """Verify request validation rejects missing model field."""
        resp = test_client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"

    def test_extra_fields_forwarded(
        self,
        test_client,
        mock_proxy_client,
        chat_completion_fixture,
    ):
        """Verify extra fields in the request are preserved (extra='allow')."""
        mock_proxy_client.forward.return_value = chat_completion_fixture

        resp = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hi"}],
                "custom_field": "custom_value",
            },
        )
        assert resp.status_code == 200

        # Verify the proxy was called (the request made it through validation)
        mock_proxy_client.forward.assert_called_once()
        call_args = mock_proxy_client.forward.call_args[0][0]
        assert call_args.model == "gpt-3.5-turbo"


# ──────────────────────── Streaming Completions ──────────────────────────


class TestChatCompletionsStreaming:
    """Tests for POST /v1/chat/completions (stream=true)."""

    def test_streaming_response(
        self,
        test_client,
        mock_proxy_client,
        stream_chunks_fixture,
        sample_streaming_request_body,
    ):
        """Verify streaming returns SSE-formatted chunks."""

        async def mock_stream(request, model_override=None):
            for chunk in stream_chunks_fixture:
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        mock_proxy_client.forward_stream = mock_stream

        resp = test_client.post(
            "/v1/chat/completions",
            json=sample_streaming_request_body,
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Parse SSE lines
        body = resp.text
        lines = [line for line in body.strip().split("\n\n") if line.startswith("data:")]

        # 4 chunks + [DONE]
        assert len(lines) == 5
        assert lines[-1].strip() == "data: [DONE]"

        # Verify first content chunk
        first_chunk = json.loads(lines[0].replace("data: ", ""))
        assert first_chunk["object"] == "chat.completion.chunk"

    def test_streaming_headers(
        self,
        test_client,
        mock_proxy_client,
        stream_chunks_fixture,
        sample_streaming_request_body,
    ):
        """Verify correct SSE headers are set."""

        async def mock_stream(request, model_override=None):
            for chunk in stream_chunks_fixture:
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        mock_proxy_client.forward_stream = mock_stream

        resp = test_client.post(
            "/v1/chat/completions",
            json=sample_streaming_request_body,
        )

        assert "text/event-stream" in resp.headers["content-type"]
        assert resp.headers.get("cache-control") == "no-cache"


# ────────────────────── Error Propagation ────────────────────────────────


class TestErrorPropagation:
    """Tests for upstream error handling."""

    def test_upstream_429_returns_429(
        self,
        test_client,
        mock_proxy_client,
        sample_request_body,
    ):
        """Verify upstream 429 is propagated to the caller."""
        mock_proxy_client.forward.side_effect = UpstreamError(
            status_code=429,
            detail="Rate limit exceeded",
        )

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)

        assert resp.status_code == 429
        err = resp.json()["error"]
        assert err["type"] == "rate_limit_error"
        assert err["code"] == "upstream_rate_limit"

    def test_upstream_500_returns_502(
        self,
        test_client,
        mock_proxy_client,
        sample_request_body,
    ):
        """An upstream 500 is the gateway's problem to report as 502 Bad Gateway."""
        mock_proxy_client.forward.side_effect = UpstreamError(
            status_code=500,
            detail="Internal server error",
        )

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)

        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "upstream_error"
        data = resp.json()
        assert data["error"]["type"] == "upstream_error"

    def test_upstream_502_returns_502(
        self,
        test_client,
        mock_proxy_client,
        sample_request_body,
    ):
        """Verify connection errors show as 502."""
        mock_proxy_client.forward.side_effect = UpstreamError(
            status_code=502,
            detail="Upstream connection error",
        )

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)

        assert resp.status_code == 502

    def test_error_response_format(
        self,
        test_client,
        mock_proxy_client,
        sample_request_body,
    ):
        """Verify error responses follow OpenAI error format."""
        mock_proxy_client.forward.side_effect = UpstreamError(
            status_code=429,
            detail="Rate limit exceeded",
        )

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)

        data = resp.json()
        assert "error" in data
        assert "message" in data["error"]
        assert "type" in data["error"]
        assert "code" in data["error"]


# ──────────────────── Telemetry Recording ────────────────────────────────


class TestTelemetryRecording:
    """What counts as a proxied request, for telemetry purposes."""

    def _count(self):
        from app.telemetry import get_summary
        return get_summary()["total_requests"] or 0

    def test_rejected_request_is_not_recorded(self, test_client):
        """A body that fails validation never reaches the proxy pipeline, so
        it must not land in telemetry — phantom rows inflate total_requests
        and, counting as cache misses, skew the adaptive threshold."""
        before = self._count()

        resp = test_client.post("/v1/chat/completions", json={"model": "gpt-3.5-turbo"})
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"

        assert self._count() == before

    def test_unknown_chat_path_is_not_recorded(self, test_client):
        """404s under the instrumented /v1/chat* prefix aren't proxied calls."""
        before = self._count()

        resp = test_client.get("/v1/chat/does-not-exist")
        assert resp.status_code == 404

        assert self._count() == before

    def test_successful_request_is_recorded(
        self, test_client, mock_proxy_client, chat_completion_fixture, sample_request_body
    ):
        """The guard must not suppress real traffic."""
        mock_proxy_client.forward.return_value = chat_completion_fixture
        before = self._count()

        resp = test_client.post("/v1/chat/completions", json=sample_request_body)
        assert resp.status_code == 200

        assert self._count() == before + 1
