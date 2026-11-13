"""Routing is opt-in: the caller's model is honoured unless they ask for routing.

Also covers keyword matching (whole-word for simple, prefix for complex) and the model allow-list.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.router import ROUTING_ALIAS, ModelRouter, Tier

CFG = "config/routing_rules.yaml"


def msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def make(mode: str = "explicit", **kw) -> ModelRouter:
    return ModelRouter(config_path=CFG, preferred_provider="openai", routing_mode=mode, **kw)


class TestModes:
    def test_explicit_honours_the_requested_model(self):
        d = make("explicit").route("gpt-4o", msgs("hi"))
        assert d.model_selected == "gpt-4o"
        assert d.applied is False

    def test_explicit_still_records_what_routing_would_have_done(self):
        d = make("explicit").route("gpt-4o", msgs("hi"))
        assert d.tier is Tier.SIMPLE
        assert d.suggested_model == "gpt-3.5-turbo"
        assert d.reason.startswith("not_applied(explicit):")

    def test_alias_routes_by_tier(self):
        router = make("explicit")
        simple = router.route(ROUTING_ALIAS, msgs("hi"))
        hard = router.route(ROUTING_ALIAS, msgs("analyze the trade-offs of these two designs"))
        assert (simple.model_selected, simple.applied) == ("gpt-3.5-turbo", True)
        assert (hard.model_selected, hard.applied) == ("gpt-4o", True)

    def test_always_routes_every_request(self):
        d = make("always").route("gpt-4o", msgs("hi"))
        assert (d.model_selected, d.applied) == ("gpt-3.5-turbo", True)

    def test_off_never_switches_a_real_model(self):
        d = make("off").route("gpt-4o", msgs("hi"))
        assert (d.model_selected, d.applied) == ("gpt-4o", False)

    def test_off_with_alias_still_resolves_to_a_real_model(self):
        """'nightjar/auto' is not a model any provider knows; it must never be sent upstream."""
        d = make("off").route(ROUTING_ALIAS, msgs("hi"))
        assert d.model_selected == "gpt-3.5-turbo"

    @pytest.mark.parametrize("mode", ["explicit", "always", "off"])
    def test_override_header_wins_in_every_mode(self, mode):
        d = make(mode).route("gpt-3.5-turbo", msgs("hi"), override_model="claude-x")
        assert (d.model_selected, d.override) == ("claude-x", True)

    def test_unknown_mode_is_rejected_at_startup(self):
        with pytest.raises(ValueError, match="routing_mode"):
            make("sometimes")

    def test_test_mode_still_forces_the_cheap_model(self):
        d = make("explicit", test_mode=True).route("gpt-4o", msgs("analyze everything"))
        assert d.model_selected == "gpt-3.5-turbo"

    def test_tier_is_reported_even_when_not_applied(self):
        d = make("explicit").route("gpt-3.5-turbo", msgs("analyze the security of this design"))
        assert d.tier is Tier.COMPLEX and d.model_selected == "gpt-3.5-turbo" and d.suggested_model == "gpt-4o"


class TestKeywordMatching:
    @pytest.mark.parametrize(
        ("prompt", "must_not_contain"),
        [
            ("Which library handles this?", "simple_keyword"),   # 'hi' inside 'which'/'this'
            ("I know the answer", "simple_keyword"),             # 'no' inside 'know'
            ("token usage question", "simple_keyword"),          # 'ok' inside 'token'
        ],
    )
    def test_simple_keywords_do_not_match_inside_words(self, prompt, must_not_contain):
        assert must_not_contain not in make("always").route("m", msgs(prompt)).reason

    def test_simple_keywords_match_whole_words(self):
        assert make("always").route("m", msgs("hi there")).reason == "simple_keyword:hi"

    @pytest.mark.parametrize("prompt", ["optimizing my query", "analysis of sales", "compared to last year",
                                        "I debugged it", "refactoring the module"])
    def test_complex_keywords_match_word_variants(self, prompt):
        d = make("always").route("m", msgs(prompt))
        assert d.tier is Tier.COMPLEX and d.reason.startswith("complex_keyword:")

    def test_complex_keyword_must_start_a_word(self):
        # Matching is anchored at the start of a word: 'implementation' contains 'implement' at
        # its start (match); 'xdebugx' has 'debug' only inside a word (no match).
        assert make("always").route("m", msgs("implementation notes")).tier is Tier.COMPLEX
        assert make("always").route("m", msgs("xdebugx")).reason.startswith("token_count")

    def test_phrase_keywords(self):
        assert make("always").route("m", msgs("explain this step by step")).reason == "complex_keyword:step by step"

    def test_only_the_head_of_a_huge_prompt_is_scanned(self):
        """A keyword after 20k characters is ignored, but such a prompt is complex by length anyway."""
        d = make("always").route("m", msgs("x" * 30_000 + " analyze"))
        assert d.tier is Tier.COMPLEX and d.reason.startswith("token_count")


class TestEndpointBehaviour:
    @pytest.fixture
    def real_router_client(self, test_client, mock_proxy_client, chat_completion_fixture):
        from app.main import app

        mock_proxy_client.forward.return_value = chat_completion_fixture
        app.state.router = make("explicit")
        return test_client

    def sent_model(self, mock_proxy_client) -> str:
        return mock_proxy_client.forward.call_args.kwargs["model_override"]

    def test_requested_model_is_what_goes_upstream(self, real_router_client, mock_proxy_client):
        resp = real_router_client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": msgs("hi")})
        assert resp.status_code == 200
        assert self.sent_model(mock_proxy_client) == "gpt-4o"
        assert resp.headers["x-model-selected"] == "gpt-4o"

    def test_alias_is_resolved_before_going_upstream(self, real_router_client, mock_proxy_client):
        resp = real_router_client.post("/v1/chat/completions", json={"model": ROUTING_ALIAS, "messages": msgs("hi")})
        assert resp.status_code == 200
        assert self.sent_model(mock_proxy_client) == "gpt-3.5-turbo"

    def test_override_header_still_works(self, real_router_client, mock_proxy_client):
        real_router_client.post(
            "/v1/chat/completions", json={"model": "gpt-4o", "messages": msgs("hi")},
            headers={"X-NightJar-Model-Override": "gpt-4o-mini"},
        )
        assert self.sent_model(mock_proxy_client) == "gpt-4o-mini"


class TestAllowList:
    @pytest.fixture
    def client(self, test_client, mock_proxy_client, chat_completion_fixture):
        from app.main import app

        mock_proxy_client.forward.return_value = chat_completion_fixture
        app.state.router = make("explicit")
        app.state.settings = Settings(allowed_models="gpt-3.5-turbo, gpt-4o-mini")
        return test_client

    def post(self, client, model="gpt-3.5-turbo", **headers):
        return client.post("/v1/chat/completions", json={"model": model, "messages": msgs("hi")}, headers=headers)

    def test_listed_model_passes(self, client):
        assert self.post(client, "gpt-4o-mini").status_code == 200

    def test_unlisted_model_is_a_403_with_a_stable_code(self, client):
        resp = self.post(client, "o1-pro")
        assert resp.status_code == 403
        err = resp.json()["error"]
        assert (err["type"], err["code"], err["param"]) == ("permission_error", "model_not_allowed", "model")

    def test_unlisted_override_is_blocked(self, client):
        resp = self.post(client, "gpt-3.5-turbo", **{"X-NightJar-Model-Override": "o1-pro"})
        assert resp.status_code == 403 and resp.json()["error"]["param"] == "X-NightJar-Model-Override"

    def test_alias_is_always_allowed(self, client):
        assert self.post(client, ROUTING_ALIAS).status_code == 200

    def test_empty_list_allows_anything(self, test_client, mock_proxy_client, chat_completion_fixture):
        from app.main import app

        mock_proxy_client.forward.return_value = chat_completion_fixture
        app.state.router = make("explicit")
        app.state.settings = Settings(allowed_models="")
        assert self.post(test_client, "anything-goes").status_code == 200
