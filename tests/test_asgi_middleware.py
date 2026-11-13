"""RequestContextMiddleware in isolation: request IDs, body limits, last-resort 500."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.asgi import RequestContextMiddleware
from app.errors import install_error_handlers

LIMIT = 1000


def make_client() -> tuple[TestClient, dict]:
    seen: dict = {"handler_ran": False}
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, max_body_bytes=LIMIT)
    install_error_handlers(app)

    @app.post("/echo")
    async def echo(request: Request):
        seen["handler_ran"] = True
        body = await request.body()
        return {"n": len(body), "request_id": request.state.request_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False), seen


class TestRequestId:
    def test_generated_when_absent(self):
        client, _ = make_client()
        rid = client.post("/echo", content=b"x").headers["x-request-id"]
        assert rid.startswith("req_") and len(rid) > 8

    def test_inbound_id_is_echoed_and_visible_to_handlers(self):
        client, _ = make_client()
        resp = client.post("/echo", content=b"x", headers={"X-Request-ID": "trace-123.abc"})
        assert resp.headers["x-request-id"] == "trace-123.abc"
        assert resp.json()["request_id"] == "trace-123.abc"

    def test_unsafe_inbound_id_is_replaced(self):
        client, _ = make_client()
        for bad in ("has space", "x" * 65, "semi;colon", "<script>"):
            rid = client.post("/echo", content=b"x", headers={"X-Request-ID": bad}).headers["x-request-id"]
            assert rid != bad and rid.startswith("req_")

    def test_ids_are_unique(self):
        client, _ = make_client()
        ids = {client.post("/echo", content=b"x").headers["x-request-id"] for _ in range(20)}
        assert len(ids) == 20


class TestBodyLimit:
    def test_under_limit_passes(self):
        client, seen = make_client()
        resp = client.post("/echo", content=b"a" * LIMIT)
        assert resp.status_code == 200 and resp.json()["n"] == LIMIT

    def test_declared_length_over_limit_is_rejected_before_the_handler(self):
        client, seen = make_client()
        resp = client.post("/echo", content=b"a" * (LIMIT + 1))
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "request_too_large"
        assert "x-request-id" in resp.headers
        assert seen["handler_ran"] is False

    def test_chunked_body_over_limit_is_rejected_while_streaming(self):
        client, _ = make_client()

        def chunks():
            for _ in range(10):
                yield b"a" * 200  # 2000 bytes total, no Content-Length

        resp = client.post("/echo", content=chunks())
        assert resp.status_code == 413
        err = resp.json()["error"]
        assert err["code"] == "request_too_large"
        assert "x-request-id" in resp.headers

    def test_chunked_body_under_limit_passes(self):
        client, _ = make_client()
        resp = client.post("/echo", content=(b"a" * 100 for _ in range(5)))
        assert resp.status_code == 200 and resp.json()["n"] == 500


class TestSafetyNet:
    def test_unhandled_error_is_json_and_keeps_request_id(self):
        client, _ = make_client()
        resp = client.get("/boom", headers={"X-Request-ID": "keep-me"})
        assert resp.status_code == 500
        assert resp.headers["x-request-id"] == "keep-me"
        assert resp.json()["error"]["type"] == "server_error"
        assert "kaboom" not in resp.text
