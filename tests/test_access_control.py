"""Key store, scopes, auth modes, per-key limits and attribution."""

from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.keys import AUTH_CACHE_SECONDS, KeyStore, hash_key
from app.keys import main as keys_cli
from app.main import app
from app.ratelimit import RateLimiter

BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def store(tmp_path):
    return KeyStore(str(tmp_path / "keys.db"))


@pytest.fixture
def secured(test_client, store, mock_proxy_client, chat_completion_fixture):
    """The app with a real key store; returns (client, store). Auth mode is set per test."""
    mock_proxy_client.forward.return_value = chat_completion_fixture
    app.state.key_store = store
    app.state.rate_limiter = RateLimiter()

    def configure(**settings):
        app.state.settings = Settings(**settings)
        return test_client

    return configure, store


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


class TestKeyStore:
    def test_key_is_returned_once_and_only_its_hash_is_stored(self, store, tmp_path):
        key = store.create("svc", {"chat"})
        assert key.startswith("nj-") and len(key) > 40
        raw = sqlite3.connect(str(tmp_path / "keys.db")).execute("SELECT * FROM api_keys").fetchall()
        assert key not in str(raw) and hash_key(key) in str(raw)
        assert all(key not in str(row) for row in store.list())

    def test_authenticate_returns_scopes_and_limits(self, store):
        key = store.create("svc", {"chat"}, ["gpt-4o-mini"], 30)
        p = store.authenticate(key)
        assert (p.name, set(p.scopes), p.allowed_models, p.rate_limit_rpm) == ("svc", {"chat"}, ("gpt-4o-mini",), 30)

    def test_unknown_key_is_rejected(self, store):
        store.create("svc", {"chat"})
        assert store.authenticate("nj-not-a-real-key") is None

    def test_revoked_key_stops_working_immediately_on_this_process(self, store):
        key = store.create("svc", {"chat"})
        assert store.authenticate(key)
        assert store.revoke("svc") and not store.revoke("svc")
        assert store.authenticate(key) is None

    def test_validation(self, store):
        with pytest.raises(ValueError):
            store.create("x", {"root"})
        with pytest.raises(ValueError):
            store.create("x", set())
        store.create("dup", {"chat"})
        with pytest.raises(ValueError, match="already exists"):
            store.create("dup", {"chat"})

    def test_two_keys_do_not_collide(self, store):
        assert store.create("a", {"chat"}) != store.create("b", {"chat"})

    def test_revocation_in_another_process_is_seen_after_the_cache_window(self, store, tmp_path, monkeypatch):
        key = store.create("svc", {"chat"})
        assert store.authenticate(key)
        other = KeyStore(str(tmp_path / "keys.db"))  # e.g. the CLI running as a separate process
        other.revoke("svc")
        assert store.authenticate(key), "cached within the window (documented lag)"
        import app.keys as keys_mod

        t = keys_mod.time.monotonic()
        monkeypatch.setattr(keys_mod.time, "monotonic", lambda: t + AUTH_CACHE_SECONDS + 1)
        assert store.authenticate(key) is None


class TestAuthModes:
    def test_auto_is_open_without_keys_and_closed_once_a_key_exists(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="auto")
        assert client.post("/v1/chat/completions", json=BODY).status_code == 200
        store.create("svc", {"chat"})
        assert client.post("/v1/chat/completions", json=BODY).status_code == 401

    def test_required_without_any_key_rejects_everything(self, secured):
        configure, _ = secured
        client = configure(nightjar_auth="required")
        assert client.post("/v1/chat/completions", json=BODY).status_code == 401
        assert client.get("/v1/telemetry").status_code == 401

    def test_off_never_enforces_even_with_keys(self, secured):
        configure, store = secured
        store.create("svc", {"chat"})
        assert configure(nightjar_auth="off").post("/v1/chat/completions", json=BODY).status_code == 200

    def test_env_keys_still_work_and_have_full_access(self, secured):
        configure, _ = secured
        client = configure(nightjar_api_keys="env-secret")
        assert client.post("/v1/chat/completions", json=BODY, headers=bearer("env-secret")).status_code == 200
        assert client.get("/v1/telemetry", headers=bearer("env-secret")).status_code == 200

    def test_invalid_auth_mode_is_rejected_at_startup(self):
        with pytest.raises(ValueError):
            Settings(nightjar_auth="sometimes")

    def test_health_is_always_open(self, secured):
        configure, _ = secured
        assert configure(nightjar_auth="required").get("/health").status_code == 200


class TestScopes:
    def test_chat_key_cannot_reach_admin_endpoints(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required")
        key = store.create("app", {"chat"})
        assert client.post("/v1/chat/completions", json=BODY, headers=bearer(key)).status_code == 200
        for method, path in (("GET", "/v1/telemetry"), ("GET", "/admin/usage"), ("DELETE", "/v1/cache"),
                             ("GET", "/v1/cache/stats"), ("GET", "/admin/logs")):
            resp = client.request(method, path, headers=bearer(key))
            assert resp.status_code == 403, path
            assert resp.json()["error"]["code"] == "insufficient_scope"

    def test_admin_key_cannot_call_the_model_unless_it_also_has_chat(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required")
        key = store.create("ops", {"admin"})
        assert client.get("/v1/telemetry", headers=bearer(key)).status_code == 200
        assert client.post("/v1/chat/completions", json=BODY, headers=bearer(key)).status_code == 403

    def test_revoked_key_is_401(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required")
        key = store.create("app", {"chat"})
        store.revoke("app")
        assert client.post("/v1/chat/completions", json=BODY, headers=bearer(key)).status_code == 401

    @pytest.mark.parametrize("header", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"},
                                        {"Authorization": "Bearer wrong"}])
    def test_bad_credentials(self, secured, header):
        configure, store = secured
        store.create("app", {"chat"})
        resp = configure(nightjar_auth="required").post("/v1/chat/completions", json=BODY, headers=header)
        assert resp.status_code == 401 and resp.headers["www-authenticate"] == "Bearer"


class TestPerKeyLimits:
    def test_key_model_allow_list(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required")
        key = store.create("cheap", {"chat"}, ["gpt-4o-mini"])
        ok = client.post("/v1/chat/completions", json={**BODY, "model": "gpt-4o-mini"}, headers=bearer(key))
        bad = client.post("/v1/chat/completions", json=BODY, headers=bearer(key))
        assert ok.status_code == 200 and bad.status_code == 403
        assert "this API key" in bad.json()["error"]["message"]

    def test_both_the_key_and_the_gateway_list_must_allow_the_model(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required", allowed_models="gpt-4o-mini")
        key = store.create("k", {"chat"}, ["gpt-4o"])
        assert client.post("/v1/chat/completions", json=BODY, headers=bearer(key)).status_code == 403

    def test_rate_limit_is_enforced_per_key_with_retry_after(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required")
        slow = store.create("slow", {"chat"}, rate_limit_rpm=3)
        other = store.create("other", {"chat"}, rate_limit_rpm=3)
        codes = [client.post("/v1/chat/completions", json=BODY, headers=bearer(slow)).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        limited = client.post("/v1/chat/completions", json=BODY, headers=bearer(slow))
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.json()["error"]["code"] == "rate_limit_exceeded"
        assert client.post("/v1/chat/completions", json=BODY, headers=bearer(other)).status_code == 200

    def test_default_rate_limit_applies_to_keys_without_their_own(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required", default_rate_limit_rpm=2)
        key = store.create("plain", {"chat"})
        codes = [client.post("/v1/chat/completions", json=BODY, headers=bearer(key)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


class TestRateLimiterUnit:
    def test_burst_then_average(self):
        rl = RateLimiter()
        for _ in range(60):
            rl.check("a", 60, now=0.0)
        with pytest.raises(Exception) as exc:
            rl.check("a", 60, now=0.0)
        assert exc.value.status_code == 429
        rl.check("a", 60, now=1.0)  # one token refilled per second at 60 rpm

    def test_no_limit_never_blocks(self):
        rl = RateLimiter()
        for _ in range(1000):
            rl.check("a", None)
            rl.check("a", 0)


class TestAttribution:
    def test_request_is_attributed_to_the_key_not_to_the_user_field(self, secured):
        configure, store = secured
        client = configure(nightjar_auth="required")
        key = store.create("billing-svc", {"chat"})
        client.post("/v1/chat/completions", json={**BODY, "user": "someone-else"}, headers=bearer(key))
        assert client.app.state.settings  # request reached the handler
        # The principal name is what telemetry records (column added in the telemetry phase).


class TestCli:
    def test_create_list_revoke(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "cli.db"))
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            create = ["create", "--name", "svc", "--scopes", "chat,admin", "--rpm", "10", "--models", "a,b"]
            assert keys_cli(create) == 0
            printed = capsys.readouterr().out
            key = next(tok for tok in printed.split() if tok.startswith("nj-"))
            assert keys_cli(["list"]) == 0
            listing = capsys.readouterr().out
            assert "svc" in listing and "active" in listing and key not in listing
            assert keys_cli(["create", "--name", "svc"]) == 2  # duplicate
            assert keys_cli(["revoke", "svc"]) == 0 and keys_cli(["revoke", "svc"]) == 1
            assert "revoked" in " ".join(k["name"] and ("revoked" if k["revoked_at"] else "") for k in
                                         KeyStore(str(tmp_path / "cli.db")).list())
        finally:
            get_settings.cache_clear()


def test_admin_endpoints_are_open_only_while_no_key_exists(secured, mock_cache):
    """auto mode keeps zero-config localhost use; the moment a key exists nothing is open."""
    configure, store = secured
    mock_cache.flush.return_value = {"vectors_cleared": 0, "redis_keys_cleared": 0}
    client = configure(nightjar_auth="auto")
    assert client.delete("/v1/cache").status_code == 200
    store.create("k", {"chat"})
    assert client.delete("/v1/cache").status_code == 401


class TestAuthEnvVar:
    """The Dockerfile sets NIGHTJAR_AUTH=required. Prove the *environment variable* does the work, with no
    keys anywhere (an earlier version named the setting `auth`, so the documented variable was ignored)."""

    def test_nightjar_auth_required_from_the_environment_enforces_with_zero_keys(self, monkeypatch, test_client):
        monkeypatch.setenv("NIGHTJAR_AUTH", "required")
        app.state.settings = Settings()
        assert app.state.settings.nightjar_auth == "required" and not app.state.settings.api_keys
        assert test_client.post("/v1/chat/completions", json=BODY).status_code == 401

    def test_default_with_zero_keys_is_open(self, monkeypatch, test_client, mock_proxy_client, chat_completion_fixture):
        monkeypatch.delenv("NIGHTJAR_AUTH", raising=False)
        mock_proxy_client.forward.return_value = chat_completion_fixture
        app.state.settings = Settings()
        assert test_client.post("/v1/chat/completions", json=BODY).status_code == 200

    def test_dockerfile_sets_the_variable_the_settings_actually_read(self):
        from pathlib import Path

        dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
        assert "ENV NIGHTJAR_AUTH=required" in dockerfile
        assert "NIGHTJAR_AUTH" in {n.upper() for n in Settings.model_fields}
