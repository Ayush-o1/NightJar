"""Tests for optional gateway bearer-token authentication (app/auth.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


@pytest.fixture
def auth_client(mock_proxy_client, mock_cache, mock_router):
    """A test client with gateway auth enabled (two valid tokens)."""
    app.state.proxy_client = mock_proxy_client
    app.state.settings = Settings(
        openai_api_key="sk-test-key-123",
        nightjar_api_keys="key-one, key-two",
    )
    app.state.cache = mock_cache
    app.state.router = mock_router
    return TestClient(app, raise_server_exceptions=False)


class TestAuthDisabledByDefault:
    """When NIGHTJAR_API_KEYS is unset, every endpoint stays open."""

    def test_chat_completions_works_without_auth_header(
        self, test_client, mock_proxy_client, chat_completion_fixture, sample_request_body
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = test_client.post("/v1/chat/completions", json=sample_request_body)
        assert resp.status_code == 200

    def test_telemetry_works_without_auth_header(self, test_client):
        resp = test_client.get("/v1/telemetry")
        assert resp.status_code == 200


class TestAuthEnabled:
    """When NIGHTJAR_API_KEYS is set, protected endpoints require it."""

    def test_missing_header_rejected(self, auth_client, sample_request_body):
        resp = auth_client.post("/v1/chat/completions", json=sample_request_body)
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_wrong_token_rejected(self, auth_client, sample_request_body):
        resp = auth_client.post(
            "/v1/chat/completions",
            json=sample_request_body,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_malformed_header_rejected(self, auth_client, sample_request_body):
        resp = auth_client.post(
            "/v1/chat/completions",
            json=sample_request_body,
            headers={"Authorization": "key-one"},  # missing "Bearer " scheme
        )
        assert resp.status_code == 401

    def test_valid_token_accepted(
        self, auth_client, mock_proxy_client, chat_completion_fixture, sample_request_body
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = auth_client.post(
            "/v1/chat/completions",
            json=sample_request_body,
            headers={"Authorization": "Bearer key-one"},
        )
        assert resp.status_code == 200

    def test_second_configured_token_also_accepted(
        self, auth_client, mock_proxy_client, chat_completion_fixture, sample_request_body
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = auth_client.post(
            "/v1/chat/completions",
            json=sample_request_body,
            headers={"Authorization": "Bearer key-two"},
        )
        assert resp.status_code == 200

    def test_health_stays_open_without_token(self, auth_client):
        """/health must never require auth — it's the readiness probe."""
        resp = auth_client.get("/health")
        assert resp.status_code == 200

    def test_telemetry_requires_token(self, auth_client):
        resp = auth_client.get("/v1/telemetry")
        assert resp.status_code == 401

    def test_cache_flush_requires_token(self, auth_client):
        resp = auth_client.delete("/v1/cache")
        assert resp.status_code == 401


class TestSettingsParsing:
    """Settings.api_keys / auth_enabled parsing."""

    def test_empty_keys_disables_auth(self):
        settings = Settings(nightjar_api_keys="")
        assert settings.auth_enabled is False
        assert settings.api_keys == []

    def test_whitespace_and_commas_trimmed(self):
        settings = Settings(nightjar_api_keys=" key-a ,key-b,, key-c")
        assert settings.api_keys == ["key-a", "key-b", "key-c"]
        assert settings.auth_enabled is True

    def test_cors_origins_default_is_none(self):
        settings = Settings()
        assert settings.cors_origins == []  # changed in 2.0: was "*"; opt in with CORS_ALLOW_ORIGINS

    def test_cors_origins_parsed_list(self):
        settings = Settings(cors_allow_origins="https://a.com, https://b.com")
        assert settings.cors_origins == ["https://a.com", "https://b.com"]
