"""OpenAI-shaped errors: bodies, upstream translation, and handlers."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import MAX_UPSTREAM_MESSAGE_CHARS, error_body, from_upstream, redact_secrets


def assert_openai_error(resp, status, etype=None, code=None):
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert isinstance(err["message"], str) and err["message"]
    if etype:
        assert err["type"] == etype
    if code:
        assert err["code"] == code
    return err


class TestUpstreamTranslation:
    @pytest.mark.parametrize(
        ("upstream", "status", "etype", "code"),
        [
            (401, 502, "upstream_error", "upstream_auth_error"),
            (403, 502, "upstream_error", "upstream_auth_error"),
            (429, 429, "rate_limit_error", "upstream_rate_limit"),
            (404, 404, "invalid_request_error", "model_not_found"),
            (400, 400, "invalid_request_error", "upstream_invalid_request"),
            (408, 504, "upstream_error", "upstream_timeout"),
            (504, 504, "upstream_error", "upstream_timeout"),
            (503, 503, "upstream_error", "upstream_unavailable"),
            (500, 502, "upstream_error", "upstream_error"),
            (418, 502, "upstream_error", "upstream_error"),
        ],
    )
    def test_status_mapping(self, upstream, status, etype, code):
        err = from_upstream(upstream, "boom")
        assert (err.status_code, err.type, err.code) == (status, etype, code)

    def test_provider_auth_failure_never_reaches_caller_as_401(self):
        """A bad provider key is the operator's problem. A 401 here would tell the caller their own key failed."""
        assert from_upstream(401, "Incorrect API key provided: sk-abcdefgh12345678").status_code != 401

    def test_provider_text_is_redacted_and_truncated(self):
        err = from_upstream(400, "bad key sk-abcdefgh12345678 " + "x" * 2000)
        assert "sk-abcdefgh12345678" not in err.message
        assert "[REDACTED]" in err.message
        assert len(err.message) < MAX_UPSTREAM_MESSAGE_CHARS + 100

    def test_auth_rejection_does_not_echo_provider_text(self):
        assert "sk-" not in from_upstream(401, "Incorrect API key provided: sk-abcdefgh12345678").message


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "key sk-proj-abcdefghijklmnop",
            "Authorization: Bearer abcdefghijklmnop1234",
            "AIzaSyA1234567890abcdefghijklmnop",
            'api_key="abcdefghijkl1234"',
        ],
    )
    def test_secrets_masked(self, text):
        assert "[REDACTED]" in redact_secrets(text)

    def test_ordinary_text_untouched(self):
        assert redact_secrets("model gpt-4o not found, try again") == "model gpt-4o not found, try again"


def test_error_body_shape():
    assert error_body("m", "t", "c", "p") == {"error": {"message": "m", "type": "t", "param": "p", "code": "c"}}


class TestHandlers:
    def test_invalid_json(self, test_client):
        resp = test_client.post("/v1/chat/completions", content=b"{bad", headers={"content-type": "application/json"})
        assert_openai_error(resp, 400, "invalid_request_error", "invalid_json")

    def test_empty_messages_rejected(self, test_client):
        resp = test_client.post("/v1/chat/completions", json={"model": "m", "messages": []})
        err = assert_openai_error(resp, 400, "invalid_request_error")
        assert err["param"] == "messages"

    def test_unknown_role_rejected(self, test_client):
        resp = test_client.post(
            "/v1/chat/completions", json={"model": "m", "messages": [{"role": "wizard", "content": "hi"}]}
        )
        err = assert_openai_error(resp, 400)
        assert "role" in err["param"]

    def test_out_of_range_temperature_rejected(self, test_client):
        resp = test_client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 9},
        )
        err = assert_openai_error(resp, 400)
        assert err["param"] == "temperature"

    def test_multiple_problems_are_counted(self, test_client):
        resp = test_client.post("/v1/chat/completions", json={})
        assert "more problem" in resp.json()["error"]["message"]

    def test_unknown_route_is_openai_shaped(self, test_client):
        assert_openai_error(test_client.get("/v1/nope"), 404, "invalid_request_error", "not_found")

    def test_wrong_method_is_openai_shaped(self, test_client):
        assert_openai_error(test_client.get("/v1/chat/completions"), 405, code="method_not_allowed")

    def test_auth_failure_shape_and_header(self, test_client):
        from app.main import app

        app.state.settings = Settings(nightjar_api_keys="secret")
        body = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        resp = test_client.post("/v1/chat/completions", json=body)
        assert_openai_error(resp, 401, "authentication_error", "invalid_api_key")
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_unexpected_exception_is_json_with_request_id(self, test_client, mock_cache, sample_request_body):
        mock_cache.lookup.side_effect = RuntimeError("secret internal detail")
        resp = test_client.post("/v1/chat/completions", json=sample_request_body)
        err = assert_openai_error(resp, 500, "server_error", "internal_error")
        assert "secret internal detail" not in resp.text
        assert resp.headers["x-request-id"] in err["message"]

    def test_valid_request_still_works_with_list_content(
        self, test_client, mock_proxy_client, chat_completion_fixture
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = test_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": "what is in this picture?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ]},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
