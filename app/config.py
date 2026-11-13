"""Pydantic Settings configuration loaded from .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration.
    All values can be overridden via environment variables or a .env file.
    """
    # --- LLM Provider Keys ---
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    mistral_api_key: str = ""
    cohere_api_key: str = ""
    xai_api_key: str = ""
    # --- Ollama ---
    ollama_base_url: str = "http://localhost:11434"
    # --- Default model tiers (used by router & compressor) ---
    simple_model: str = "gpt-3.5-turbo"
    complex_model: str = "gpt-4o"
    # --- Redis (response payloads for the cache). Include a password in the URL if needed:
    #     redis://:password@host:6379 ---
    redis_url: str = "redis://localhost:6379"
    # --- Response cache ---
    # The exact tier is always on. The semantic tier is opt-in because embedding similarity can
    # return a wrong answer (see docs/benchmarks/CACHE_CORRECTNESS.md).
    semantic_cache: bool = False
    # Minimum cosine similarity for a semantic hit (only used when semantic_cache).
    similarity_threshold: float = 0.92
    cache_ttl_seconds: int = 86400
    # Requests with a temperature above this are not cached. An unset temperature counts as 1.0.
    cache_max_temperature: float = 1.0
    # Cap on vectors held by the semantic tier (oldest evicted first).
    cache_max_entries: int = 50_000
    cache_persist_interval_seconds: int = 60
    # --- Context Compression ---
    compress_threshold: int = 2000
    compress_keep_recent: int = 4
    compress_min_turns: int = 6
    compress_summary_model: str = "gpt-3.5-turbo"
    # --- Model Routing ---
    preferred_provider: str = "openai"
    # explicit (default): honour the model the caller asked for; route only for the alias
    # "nightjar/auto" or the override header. always: route every request. off: never switch.
    routing_mode: str = "explicit"
    # Comma-separated allow-list for caller-chosen models (requested model and the override
    # header). Empty = any model. Tier models configured by the operator are always allowed.
    allowed_models: str = ""
    # --- Logging ---
    log_level: str = "INFO"
    # --- Storage Paths ---
    sqlite_db_path: str = "./data/telemetry.db"
    vector_index_path: str = "./data/vectors.npz"
    # --- Test Mode ---
    test_mode: bool = False
    # --- OpenTelemetry ---
    enable_otel: bool = False                            # set True in .env to activate
    otel_endpoint: str = "http://localhost:4317"         # OTLP gRPC collector endpoint
    # --- Gateway Authentication ---
    # Comma-separated list of accepted bearer tokens for calls to this gateway
    # (checked against the `Authorization: Bearer <token>` header, distinct
    # from the upstream provider keys above). Empty = auth disabled, which is
    # the right default for local single-user development. Set this before
    # exposing NightJar beyond localhost.
    nightjar_api_keys: str = ""
    # Deprecated: the pre-rename name (CONTEXTFORGE_API_KEYS). Still read so
    # existing deployments keep working; remove in the next minor release.
    contextforge_api_keys: str = ""
    # --- Request limits ---
    # Hard cap on the request body, enforced before the body is read (413).
    max_request_bytes: int = 8 * 1024 * 1024
    # Cap on characters of *text* across all messages (400, context_length_exceeded).
    # Image/audio payloads don't count; they are not tokenized or embedded.
    max_prompt_chars: int = 2_000_000
    # --- CORS ---
    # Comma-separated list of allowed origins, or "*" for any origin.
    # Credentialed CORS (cookies) is never enabled here — this gateway is
    # authenticated via bearer token, not cookies, so wildcard origins are
    # safe without allow_credentials.
    cors_allow_origins: str = "*"

    @property
    def api_keys(self) -> list[str]:
        """Parsed, non-empty list of accepted gateway bearer tokens."""
        raw = ",".join([self.nightjar_api_keys, self.contextforge_api_keys])
        return [k.strip() for k in raw.split(",") if k.strip()]

    @property
    def allowed_model_list(self) -> list[str]:
        return [m.strip() for m in self.allowed_models.split(",") if m.strip()]

    @property
    def deprecated_settings_in_use(self) -> list[str]:
        """Names of deprecated settings that currently have a value."""
        return ["CONTEXTFORGE_API_KEYS"] if self.contextforge_api_keys.strip() else []

    @property
    def auth_enabled(self) -> bool:
        """Whether gateway bearer-token auth is active."""
        return bool(self.api_keys)

    @property
    def cors_origins(self) -> list[str]:
        """Parsed CORS allow-origins list."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
