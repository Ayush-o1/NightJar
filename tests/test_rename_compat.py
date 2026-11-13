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


class TestDocumentedEnvVarNames:
    """Setting field names are the env var names. These are the names the docs and .env.example promise."""

    def test_semantic_cache_env_var(self, monkeypatch):
        assert Settings().semantic_cache is False
        monkeypatch.setenv("SEMANTIC_CACHE", "true")
        assert Settings().semantic_cache is True

    @pytest.mark.parametrize(("env", "attr", "value", "expected"), [
        ("ROUTING_MODE", "routing_mode", "always", "always"),
        ("ALLOWED_MODELS", "allowed_models", "a,b", "a,b"),
        ("CACHE_MAX_TEMPERATURE", "cache_max_temperature", "0.3", 0.3),
        ("CACHE_MAX_ENTRIES", "cache_max_entries", "10", 10),
        ("VECTOR_INDEX_PATH", "vector_index_path", "/x/v.npz", "/x/v.npz"),
        ("MAX_REQUEST_BYTES", "max_request_bytes", "1024", 1024),
        ("MAX_PROMPT_CHARS", "max_prompt_chars", "99", 99),
        ("SIMILARITY_THRESHOLD", "similarity_threshold", "0.9", 0.9),
    ])
    def test_new_settings_are_readable_from_their_documented_env_vars(self, monkeypatch, env, attr, value, expected):
        monkeypatch.setenv(env, value)
        assert getattr(Settings(), attr) == expected

    def test_every_variable_in_env_example_is_a_real_setting(self):
        """Catches docs that promise a variable the code ignores (this is how SEMANTIC_CACHE was caught)."""
        import re
        from pathlib import Path

        text = (Path(__file__).parent.parent / ".env.example").read_text()
        documented = set(re.findall(r"(?m)^([A-Z][A-Z0-9_]+)=", text))
        fields = {name.upper() for name in Settings.model_fields}
        assert documented - fields == set(), f"in .env.example but not a setting: {documented - fields}"
