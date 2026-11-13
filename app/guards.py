"""Lexical guards for the semantic cache tier.

Embedding similarity is high for prompts that differ in exactly the word that
matters ("2022" vs "2023", "French" vs "Spanish", "not"). These checks reject a
semantic hit when the two prompts disagree on things an embedding blurs:

* numbers
* negation words
* quoted strings and code spans
* capitalised words that are not at the start of a sentence (names, places, languages)

They are deliberately cheap and conservative. They do **not** catch swaps of
ordinary lowercase words ("ascending"/"descending", "miles"/"kilometers",
"ibuprofen"/"acetaminophen"). The measured false-hit rate with and without the
guards is in docs/benchmarks/CACHE_CORRECTNESS.md; that is the number to trust,
not this docstring.
"""

from __future__ import annotations

import re
from collections import Counter

_NUMBER = re.compile(r"\d+(?:[.,:/\-]\d+)*")
_NEGATIONS = frozenset({
    "not", "no", "never", "without", "none", "neither", "nor", "cannot", "cant", "dont", "doesnt",
    "isnt", "arent", "wont", "wasnt", "werent", "shouldnt", "wouldnt", "couldnt", "didnt", "nothing",
})
_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”|`([^`]+)`|'([^']{2,})'")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_SENTENCE_END = re.compile(r"[.!?]\s*$")


def _numbers(text: str) -> Counter:
    return Counter(_NUMBER.findall(text))


def _negations(text: str) -> frozenset[str]:
    words = {w.lower().replace("'", "").replace("’", "") for w in _WORD.findall(text)}
    return frozenset(words & _NEGATIONS)


def _quoted(text: str) -> Counter:
    return Counter(next(g for g in m.groups() if g).strip().lower() for m in _QUOTED.finditer(text))


def _proper_nouns(text: str) -> frozenset[str]:
    """Capitalised words that are not the first word of the text or of a sentence."""
    found: set[str] = set()
    sentence_start = True
    for token in re.finditer(r"[A-Za-z][A-Za-z'’\-]*|[.!?]+", text):
        t = token.group(0)
        if _SENTENCE_END.search(t) and not t[0].isalpha():
            sentence_start = True
            continue
        if t[0].isupper() and not sentence_start and t != "I" and not t.startswith("I'"):
            found.add(t.lower())
        sentence_start = False
    return frozenset(found)


def disagreement(cached_prompt: str, new_prompt: str) -> str | None:
    """Return the name of the first guard that the two prompts disagree on, else None."""
    if _numbers(cached_prompt) != _numbers(new_prompt):
        return "numbers"
    if _negations(cached_prompt) != _negations(new_prompt):
        return "negation"
    if _quoted(cached_prompt) != _quoted(new_prompt):
        return "quoted_text"
    if _proper_nouns(cached_prompt) != _proper_nouns(new_prompt):
        return "proper_nouns"
    return None
