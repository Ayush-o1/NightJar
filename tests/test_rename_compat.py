"""Backward compatibility for names changed in the ContextForge → NightJar rename.

Anything asserted here is a promise to existing deployments. When the
deprecation window closes, delete this file together with app/compat.py.
"""

from __future__ import annotations

import pytest

from app.config import Settings


class TestLegacyApiKeysSetting:
    def test_legacy_env_name_still_enables_auth(self):
        settings = Settings(contextforge_api_keys="old-key")
        assert settings.auth_enabled
        assert settings.api_keys == ["old-key"]

    def test_legacy_use_is_reported(self):
        assert Settings(contextforge_api_keys="old-key").deprecated_settings_in_use == ["CONTEXTFORGE_API_KEYS"]

    def test_new_name_alone_reports_nothing(self):
        settings = Settings(nightjar_api_keys="new-key")
        assert settings.deprecated_settings_in_use == []

    def test_both_names_are_merged(self):
        settings = Settings(nightjar_api_keys="new-key", contextforge_api_keys="old-key")
        assert settings.api_keys == ["new-key", "old-key"]

    def test_env_var_old_name_is_read(self, monkeypatch):
        monkeypatch.setenv("CONTEXTFORGE_API_KEYS", "from-env")
        assert Settings().api_keys == ["from-env"]

    def test_env_var_new_name_is_read(self, monkeypatch):
        monkeypatch.setenv("NIGHTJAR_API_KEYS", "from-env")
        assert Settings().api_keys == ["from-env"]


class TestHeaderAliases:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("X-NightJar-Model-Override", "gpt-4o"),
            ("X-ContextForge-Model-Override", "gpt-4o"),
        ],
    )
    def test_override_header_both_spellings(
        self, test_client, mock_router, mock_proxy_client, chat_completion_fixture, sample_request_body,
        header, expected,
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = test_client.post("/v1/chat/completions", json=sample_request_body, headers={header: expected})
        assert resp.status_code == 200
        assert mock_router.route.call_args.kwargs["override_model"] == expected

    def test_new_header_wins_when_both_sent(
        self, test_client, mock_router, mock_proxy_client, chat_completion_fixture, sample_request_body
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        resp = test_client.post(
            "/v1/chat/completions",
            json=sample_request_body,
            headers={"X-NightJar-Model-Override": "new-model", "X-ContextForge-Model-Override": "old-model"},
        )
        assert resp.status_code == 200
        assert mock_router.route.call_args.kwargs["override_model"] == "new-model"

    def test_no_override_header_means_none(
        self, test_client, mock_router, mock_proxy_client, chat_completion_fixture, sample_request_body
    ):
        mock_proxy_client.forward.return_value = chat_completion_fixture
        test_client.post("/v1/chat/completions", json=sample_request_body)
        assert mock_router.route.call_args.kwargs["override_model"] is None
