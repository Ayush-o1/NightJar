"""Fingerprints decide what counts as 'the same request'. Wrong merges are wrong cache hits."""

from __future__ import annotations

import pytest

from app.fingerprint import build_fingerprint, response_is_cacheable

U = lambda t: {"role": "user", "content": t}  # noqa: E731
S = lambda t: {"role": "system", "content": t}  # noqa: E731


def fp(messages, model="gpt-4o", **params):
    return build_fingerprint({"model": model, "messages": messages, **params}, messages, model)


class TestIdentity:
    def test_identical_requests_share_a_key(self):
        a, b = fp([S("be brief"), U("hi")], temperature=0), fp([S("be brief"), U("hi")], temperature=0)
        assert a.exact_key == b.exact_key and a.partition == b.partition

    def test_param_order_and_ignored_fields_do_not_matter(self):
        a = fp([U("hi")], temperature=0, top_p=1)
        b = build_fingerprint(
            {"top_p": 1, "temperature": 0, "stream": False, "user": "alice", "metadata": {"x": 1}, "messages": []},
            [U("hi")], "gpt-4o",
        )
        assert a.exact_key == b.exact_key

    @pytest.mark.parametrize(
        "other",
        [
            dict(model="gpt-4o-mini"),
            dict(temperature=0.2),
            dict(max_tokens=50),
            dict(top_p=0.5),
            dict(stop=["\n"]),
            dict(seed=7),
            dict(response_format={"type": "json_object"}),
            dict(some_new_provider_field="x"),  # unknown fields count: ignoring one could merge different requests
        ],
    )
    def test_anything_that_changes_the_answer_changes_the_key(self, other):
        base = fp([U("hi")], temperature=0)
        model = other.pop("model", "gpt-4o")
        params = {"temperature": 0, **other}
        changed = fp([U("hi")], model=model, **params)
        assert changed.exact_key != base.exact_key

    def test_system_prompt_changes_both_keys(self):
        a, b = fp([S("pirate"), U("hi")]), fp([S("lawyer"), U("hi")])
        assert a.exact_key != b.exact_key and a.partition != b.partition

    def test_history_changes_the_partition(self):
        a = fp([U("a"), {"role": "assistant", "content": "x"}, U("q")])
        b = fp([U("b"), {"role": "assistant", "content": "x"}, U("q")])
        assert a.partition != b.partition

    def test_only_the_last_message_differs_within_a_partition(self):
        a, b = fp([S("s"), U("first question")]), fp([S("s"), U("second question")])
        assert a.partition == b.partition and a.exact_key != b.exact_key

    def test_none_valued_params_are_ignored(self):
        assert fp([U("hi")], temperature=None).exact_key == fp([U("hi")]).exact_key


class TestEligibility:
    def test_plain_request_is_eligible(self):
        assert fp([U("hi")]).eligible

    @pytest.mark.parametrize(
        ("params", "reason"),
        [
            (dict(n=2), "n_greater_than_1"),
            (dict(tools=[{"type": "function"}]), "tools"),
            (dict(functions=[{}]), "tools"),
            (dict(tool_choice="auto"), "tools"),
            (dict(temperature=1.5), "temperature_above_cutoff"),
        ],
    )
    def test_ineligible_requests(self, params, reason):
        result = fp([U("hi")], **params)
        assert not result.eligible and result.reason == reason

    def test_unset_temperature_counts_as_default_and_follows_the_cutoff(self):
        assert build_fingerprint({}, [U("hi")], "m", max_temperature=1.0).eligible
        assert not build_fingerprint({}, [U("hi")], "m", max_temperature=0.5).eligible

    def test_empty_messages(self):
        assert not build_fingerprint({}, [], "m").eligible


class TestSemanticText:
    def test_plain_user_message(self):
        assert fp([S("s"), U("what is 2+2")]).semantic_text == "what is 2+2"

    def test_text_parts_are_joined(self):
        m = {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        assert fp([m]).semantic_text == "a\nb"

    def test_media_disables_the_semantic_tier_but_not_the_exact_tier(self):
        m = {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {}}]}
        result = fp([m])
        assert result.eligible and result.semantic_text is None

    def test_media_earlier_in_the_conversation_also_disables_it(self):
        img = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        assert fp([img, {"role": "assistant", "content": "ok"}, U("and now?")]).semantic_text is None

    def test_last_message_not_from_user(self):
        assert fp([U("q"), {"role": "assistant", "content": "a"}]).semantic_text is None


class TestResponseCacheability:
    def good(self, **over):
        r = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}]}
        r["choices"][0].update(over)
        return r

    def test_complete_text_answer(self):
        assert response_is_cacheable(self.good()) == (True, "ok")

    @pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", None])
    def test_truncated_or_unusual_finish_is_not_stored(self, finish):
        assert not response_is_cacheable(self.good(finish_reason=finish))[0]

    def test_tool_calls_are_not_stored(self):
        r = self.good(message={"role": "assistant", "content": "x", "tool_calls": [{}]})
        assert response_is_cacheable(r) == (False, "tool_calls")

    def test_empty_content_is_not_stored(self):
        assert not response_is_cacheable(self.good(message={"role": "assistant", "content": "  "}))[0]

    def test_multiple_choices_are_not_stored(self):
        r = self.good()
        r["choices"].append(r["choices"][0])
        assert response_is_cacheable(r) == (False, "not_single_choice")
