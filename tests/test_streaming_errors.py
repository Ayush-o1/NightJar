"""Streaming: errors before the first byte are HTTP errors; errors after it are in-band events."""

from __future__ import annotations

import asyncio
import json

from app import telemetry as tel
from app.proxy import UpstreamError

BODY = {"model": "gpt-3.5-turbo", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


def sse_events(text: str) -> list[str]:
    return [line[len("data: "):] for line in text.split("\n\n") if line.startswith("data: ")]


class TestConnectTimeFailures:
    """The first chunk is awaited before the response is created, so the status code is still ours to set."""

    def test_upstream_429_before_first_chunk_is_a_real_429(self, test_client, mock_proxy_client):
        async def failing(*a, **k):
            raise UpstreamError(429, "slow down")
            yield  # pragma: no cover - makes this an async generator

        mock_proxy_client.forward_stream = failing
        resp = test_client.post("/v1/chat/completions", json=BODY)
        assert resp.status_code == 429
        assert resp.json()["error"]["type"] == "rate_limit_error"

    def test_provider_key_rejected_is_502_not_401(self, test_client, mock_proxy_client):
        async def failing(*a, **k):
            raise UpstreamError(401, "Incorrect API key provided: sk-abcdefgh12345678")
            yield  # pragma: no cover

        mock_proxy_client.forward_stream = failing
        resp = test_client.post("/v1/chat/completions", json=BODY)
        assert resp.status_code == 502
        assert "sk-abcdefgh12345678" not in resp.text


class TestMidStreamFailures:
    def test_error_after_first_chunk_is_sent_in_band(self, test_client, mock_proxy_client):
        async def broken(*a, **k):
            yield 'data: {"id":"c1","choices":[{"delta":{"content":"Hel"}}]}\n\n'
            raise UpstreamError(500, "provider exploded")

        mock_proxy_client.forward_stream = broken
        resp = test_client.post("/v1/chat/completions", json=BODY)
        assert resp.status_code == 200  # headers were already out
        events = sse_events(resp.text)
        assert json.loads(events[0])["id"] == "c1"
        err = json.loads(events[-1])["error"]
        assert err["type"] == "upstream_error"
        assert "[DONE]" not in events  # a failed stream must not look like a finished one

    def test_non_upstream_exception_mid_stream_is_contained(self, test_client, mock_proxy_client):
        async def broken(*a, **k):
            yield 'data: {"id":"c1"}\n\n'
            raise ConnectionResetError("socket closed")

        mock_proxy_client.forward_stream = broken
        resp = test_client.post("/v1/chat/completions", json=BODY)
        assert resp.status_code == 200
        assert json.loads(sse_events(resp.text)[-1])["error"]["code"] == "stream_interrupted"
        assert "socket closed" not in resp.text

    def test_upstream_stream_is_closed_even_after_failure(self, test_client, mock_proxy_client):
        closed = {"v": False}

        class Stream:
            def __aiter__(self):
                return self

            def __init__(self):
                self.sent = False

            async def __anext__(self):
                if not self.sent:
                    self.sent = True
                    return 'data: {"id":"c1"}\n\n'
                raise UpstreamError(500, "x")

            async def aclose(self):
                closed["v"] = True

        mock_proxy_client.forward_stream = lambda *a, **k: Stream()
        test_client.post("/v1/chat/completions", json=BODY)
        assert closed["v"] is True


class TestHappyStream:
    def test_chunks_pass_through_unchanged(self, test_client, mock_proxy_client):
        async def ok(*a, **k):
            yield 'data: {"id":"c1"}\n\n'
            yield 'data: {"id":"c2"}\n\n'
            yield "data: [DONE]\n\n"

        mock_proxy_client.forward_stream = ok
        resp = test_client.post("/v1/chat/completions", json=BODY)
        assert resp.status_code == 200
        assert sse_events(resp.text) == ['{"id":"c1"}', '{"id":"c2"}', "[DONE]"]
        assert resp.headers["x-model-selected"]
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_empty_upstream_stream_yields_an_empty_200(self, test_client, mock_proxy_client):
        async def nothing(*a, **k):
            return
            yield  # pragma: no cover

        mock_proxy_client.forward_stream = nothing
        assert test_client.post("/v1/chat/completions", json=BODY).status_code == 200


class TestStreamingTelemetry:
    def test_latency_covers_the_whole_stream_not_just_the_headers(self, test_client, mock_proxy_client):
        async def slow(*a, **k):
            for i in range(3):
                await asyncio.sleep(0.15)
                yield f'data: {{"id":"c{i}"}}\n\n'
            yield "data: [DONE]\n\n"

        mock_proxy_client.forward_stream = slow
        before = tel.get_summary()["total_requests"]
        resp = test_client.post("/v1/chat/completions", json=BODY)
        assert resp.status_code == 200

        rec = tel.get_records(1)[0]
        assert tel.get_summary()["total_requests"] == before + 1
        assert rec["request_id"] == resp.headers["x-request-id"]
        assert rec["latency_ms"] >= 400, "latency should span the ~450 ms the stream took"
