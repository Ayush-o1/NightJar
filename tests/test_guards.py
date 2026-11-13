"""Semantic-hit guards: what they catch, and (just as important) what they do not."""

from __future__ import annotations

import pytest

from app.guards import disagreement


class TestCatches:
    @pytest.mark.parametrize(
        ("a", "b", "guard"),
        [
            ("What was the revenue in 2022?", "What was the revenue in 2023?", "numbers"),
            ("Convert 10 miles to km", "Convert 100 miles to km", "numbers"),
            ("Is it safe to take this with alcohol?", "Is it not safe to take this with alcohol?", "negation"),
            ("I can do this", "I cannot do this", "negation"),
            ('Translate "good morning" to French', 'Translate "good evening" to French', "quoted_text"),
            ("Explain `foo()` in this code", "Explain `bar()` in this code", "quoted_text"),
            ("Translate this to French", "Translate this to Spanish", "proper_nouns"),
            ("How far is Paris from London?", "How far is Paris from Berlin?", "proper_nouns"),
        ],
    )
    def test_disagreement_is_named(self, a, b, guard):
        assert disagreement(a, b) == guard


class TestAllows:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("What is the capital of France?", "what's the capital of France"),
            ("How do I reset my password?", "How can I reset my password?"),
            ("Tell me a joke about cats", "Tell me a joke about cats."),
            ("Sort 3 numbers: 5, 2, 9", "Sort these 3 numbers: 5, 2, 9"),
        ],
    )
    def test_equivalent_prompts_pass(self, a, b):
        assert disagreement(a, b) is None

    def test_sentence_initial_capital_is_not_a_proper_noun(self):
        assert disagreement("Explain recursion", "explain recursion") is None


class TestKnownMisses:
    """These pairs mean different things and the guards cannot tell. If one of these starts failing
    (guard fires), that is an improvement: update docs/benchmarks/CACHE_CORRECTNESS.md and this test."""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Convert 10 miles to kilometers", "Convert 10 kilometers to miles"),
            ("Write a function that sorts a list ascending", "Write a function that sorts a list descending"),
            ("Is it safe to take ibuprofen with alcohol?", "Is it safe to take acetaminophen with alcohol?"),
        ],
    )
    def test_lowercase_swaps_slip_through(self, a, b):
        assert disagreement(a, b) is None
