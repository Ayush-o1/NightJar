"""Pydantic request/response schemas matching the OpenAI /v1/chat/completions spec."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app import __version__

# ──────────────────────────────── Request ────────────────────────────────


class ChatMessage(BaseModel):
    """A single message in the conversation."""

    role: str
    content: str | None = None
    name: str | None = None
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request body.

    Only the fields required for passthrough are defined; extra fields
    are forwarded transparently via model_config.
    """

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    n: int | None = None
    stream: bool = False
    stop: str | list[str] | None = None
    max_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None

    model_config = {"extra": "allow"}


# ──────────────────────────────── Response ───────────────────────────────


class UsageInfo(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    model_config = {"extra": "allow"}


class ChoiceMessage(BaseModel):
    """Message returned in a completion choice."""

    role: str = "assistant"
    content: str | None = None
    tool_calls: list[Any] | None = None

    model_config = {"extra": "allow"}


class Choice(BaseModel):
    """A single completion choice."""

    index: int = 0
    message: ChoiceMessage
    finish_reason: str | None = None

    model_config = {"extra": "allow"}


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    id: str = ""
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: list[Choice] = Field(default_factory=list)
    usage: UsageInfo | None = None
    system_fingerprint: str | None = None

    model_config = {"extra": "allow"}


# ─────────────────────────── Streaming (SSE) ─────────────────────────────


class DeltaContent(BaseModel):
    """Delta content in a streaming chunk."""

    role: str | None = None
    content: str | None = None
    tool_calls: list[Any] | None = None

    model_config = {"extra": "allow"}


class StreamChoice(BaseModel):
    """A single streaming choice."""

    index: int = 0
    delta: DeltaContent
    finish_reason: str | None = None

    model_config = {"extra": "allow"}


class ChatCompletionChunk(BaseModel):
    """A single SSE chunk in a streaming response."""

    id: str = ""
    object: str = "chat.completion.chunk"
    created: int = 0
    model: str = ""
    choices: list[StreamChoice] = Field(default_factory=list)
    system_fingerprint: str | None = None

    model_config = {"extra": "allow"}


# ─────────────────────────── Health Check ────────────────────────────────


class HealthResponse(BaseModel):
    """Health check response.

    ``status`` reflects overall liveness (the process is up and serving
    requests) and stays "ok" even if an optional dependency like Redis is
    down, since the semantic cache degrades gracefully (see app/cache.py).
    ``redis`` reports that dependency's reachability separately so callers
    (and the dashboard) can tell a healthy-but-degraded gateway apart from
    one that's actually failing to serve requests.
    """

    status: str = "ok"
    version: str = __version__
    redis: str = "unknown"
