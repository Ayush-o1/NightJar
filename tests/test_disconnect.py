"""When a streaming client goes away, NightJar must stop the upstream call (it is being paid for).

Needs a real socket: in-process ASGI transports don't model a client hanging up mid-response.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import app


@pytest.fixture
def live_gateway(fake_provider):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_client_disconnect_mid_stream_closes_the_upstream_stream(live_gateway, provider):
    provider.config["stream_delay_ms"] = 200  # ~10 chunks at 200 ms: plenty of time to hang up
    body = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "a long answer please " * 3}]}
    url = f"{live_gateway}/v1/chat/completions"
    with httpx.Client(timeout=30) as client, client.stream("POST", url, json=body) as r:
        assert r.status_code == 200
        first = next(r.iter_lines())
        assert first.startswith("data:")
    # leaving the `with` closed the connection after one chunk
    assert wait_for(lambda: provider.stats["stream_aborted"] == 1), (
        f"the provider stream was never cancelled: {provider.stats}"
    )
    health = httpx.get(f"{live_gateway}/health").json()
    assert wait_for(lambda: httpx.get(f"{live_gateway}/health").json()["upstream"]["in_flight"] == 0), health


def test_completed_stream_is_not_counted_as_aborted(live_gateway, provider):
    body = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "short"}]}
    resp = httpx.post(f"{live_gateway}/v1/chat/completions", json=body, timeout=30)
    assert resp.text.rstrip().endswith("data: [DONE]")
    assert provider.stats["stream_aborted"] == 0
    assert wait_for(lambda: httpx.get(f"{live_gateway}/health").json()["upstream"]["in_flight"] == 0)
