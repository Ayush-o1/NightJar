"""Message-text extraction and bounded token counting.

Why bounded: tiktoken's BPE is quadratic on long runs without whitespace
(measured in docs/BASELINE.md: 200 KB of "a" takes 8.6 s, 10 MB never finishes
in practice). Token counts here only feed routing and compression *thresholds*
that are a few hundred to a few thousand tokens, so beyond a cap we estimate
instead of encoding. Cost of the cap: counts for very large messages are
approximate (about 4 characters per token), which is irrelevant to those
thresholds.
"""

from __future__ import annotations

from typing import Any

import tiktoken

# Characters of a single text we are willing to hand to the tokenizer.
# 8,000 whitespace-free characters encode in roughly 10 ms.
MAX_EXACT_CHARS = 8_000
CHARS_PER_TOKEN_ESTIMATE = 4
PER_MESSAGE_OVERHEAD = 4  # role + separators in the OpenAI chat format

_encodings: dict[str, tiktoken.Encoding] = {}


def _encoding_for(model: str) -> tiktoken.Encoding:
    enc = _encodings.get(model)
    if enc is None:
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        _encodings[model] = enc
    return enc


def content_to_text(content: Any) -> str:
    """Flatten OpenAI message ``content`` (string, list of parts, or None) to text.

    Text parts are joined with newlines. Non-text parts (images, audio) become a
    short placeholder so they still influence length and cache keys, but their
    payload (often a huge base64 string) is never tokenized or embedded.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text":
                    parts.append(str(part.get("text", "")))
                elif ptype:
                    parts.append(f"[{ptype}]")
        return "\n".join(parts)
    return str(content)


def count_text_tokens(text: str, model: str = "gpt-3.5-turbo") -> int:
    """Token count for ``text``: exact up to MAX_EXACT_CHARS, estimated beyond."""
    if len(text) <= MAX_EXACT_CHARS:
        return len(_encoding_for(model).encode(text))
    head = len(_encoding_for(model).encode(text[:MAX_EXACT_CHARS]))
    return head + (len(text) - MAX_EXACT_CHARS) // CHARS_PER_TOKEN_ESTIMATE


def count_message_tokens(messages: list[dict], model: str = "gpt-3.5-turbo") -> int:
    """Total tokens across messages, including tool-call payloads."""
    total = 0
    for msg in messages:
        total += count_text_tokens(content_to_text(msg.get("content")), model) + PER_MESSAGE_OVERHEAD
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += count_text_tokens(str(tool_calls), model)
    return total


def total_text_chars(messages: list[dict]) -> int:
    """Cheap upper-level size check: characters of text across all messages."""
    return sum(len(content_to_text(m.get("content"))) for m in messages)
