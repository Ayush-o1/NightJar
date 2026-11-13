"""Rule-based prompt complexity classifier and model router.

Classifies prompts as SIMPLE or COMPLEX based on token count thresholds
and keyword signals, then selects the appropriate model tier.
Uses tiktoken for accurate model-specific token counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import structlog
import tiktoken
import yaml

logger = structlog.get_logger()


class Tier(str, Enum):
    """Complexity tier for routing decisions."""

    SIMPLE = "simple"
    COMPLEX = "complex"


@dataclass
class RoutingDecision:
    """Result of the routing classification."""

    tier: Tier
    model_requested: str
    model_selected: str
    reason: str
    token_count: int
    override: bool = False


@dataclass
class RoutingConfig:
    """Parsed routing configuration from YAML."""

    simple_max_tokens: int = 200
    complex_min_tokens: int = 500
    complex_keywords: list[str] = field(default_factory=list)
    simple_keywords: list[str] = field(default_factory=list)
    model_map: dict[str, dict[str, str]] = field(default_factory=dict)
    default_model: str = "gpt-3.5-turbo"


class ModelRouter:
    """Classifies prompt complexity and selects the appropriate model."""

    def __init__(
        self,
        config_path: str = "config/routing_rules.yaml",
        preferred_provider: str = "openai",
        test_mode: bool = False,
        simple_model: str | None = None,
        complex_model: str | None = None,
    ) -> None:
        self._preferred_provider = preferred_provider
        self._test_mode = test_mode
        self._config = self._load_config(config_path)
        # Runtime overrides take precedence over YAML model_map values
        if simple_model:
            self._config.model_map.setdefault(preferred_provider, {})["simple"] = simple_model
        if complex_model:
            self._config.model_map.setdefault(preferred_provider, {})["complex"] = complex_model
        logger.info(
            "router.loaded",
            provider=preferred_provider,
            simple_max_tokens=self._config.simple_max_tokens,
            complex_keywords_count=len(self._config.complex_keywords),
            simple_model=self._config.model_map.get(preferred_provider, {}).get("simple"),
            complex_model=self._config.model_map.get(preferred_provider, {}).get("complex"),
            test_mode=test_mode,
        )

    @staticmethod
    def _load_config(config_path: str) -> RoutingConfig:
        """Load and parse the routing configuration YAML."""
        path = Path(config_path)
        if not path.exists():
            logger.warning("router.config_not_found", path=config_path)
            return RoutingConfig()

        with open(path) as f:
            raw = yaml.safe_load(f)

        config = RoutingConfig()

        rules = raw.get("rules", {})
        simple = rules.get("simple", {})
        complex_rule = rules.get("complex", {})

        config.simple_max_tokens = simple.get("max_tokens", 200)
        config.simple_keywords = [kw.lower() for kw in simple.get("keywords", [])]
        config.complex_min_tokens = complex_rule.get("min_tokens", 500)
        config.complex_keywords = [kw.lower() for kw in complex_rule.get("keywords", [])]

        config.model_map = raw.get("model_map", {})
        config.default_model = raw.get("default_model", "gpt-3.5-turbo")

        return config

    def count_tokens(self, text: str, model: str = "gpt-3.5-turbo") -> int:
        """Count tokens using tiktoken with model-specific encoding."""
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))

    def _flatten_messages(self, messages: list[dict]) -> str:
        """Flatten messages list into a single text string for analysis."""
        parts = []
        for msg in messages:
            content = msg.get("content", "") or ""
            if content:
                parts.append(content)
        return "\n".join(parts)

    def _classify(self, text: str, token_count: int) -> tuple[Tier, str]:
        """Classify a prompt as SIMPLE or COMPLEX with a reason.

        Priority:
          1. Complex keyword match → COMPLEX
          2. High token count → COMPLEX
          3. Simple keyword match + low token count → SIMPLE
          4. Low token count → SIMPLE
          5. Ambiguous → COMPLEX (safer default)
        """
        text_lower = text.lower()

        # Check complex keywords first (high priority)
        for keyword in self._config.complex_keywords:
            if keyword in text_lower:
                return Tier.COMPLEX, f"complex_keyword:{keyword}"

        # High token count → complex
        if token_count >= self._config.complex_min_tokens:
            return Tier.COMPLEX, f"token_count:{token_count}>={self._config.complex_min_tokens}"

        # Check simple keywords
        for keyword in self._config.simple_keywords:
            if keyword in text_lower:
                if token_count <= self._config.simple_max_tokens:
                    return Tier.SIMPLE, f"simple_keyword:{keyword}"

        # Low token count → simple
        if token_count <= self._config.simple_max_tokens:
            return Tier.SIMPLE, f"token_count:{token_count}<={self._config.simple_max_tokens}"

        # Ambiguous: default to COMPLEX (safer)
        return Tier.COMPLEX, "ambiguous_default"

    def _select_model(self, tier: Tier, model_requested: str) -> str:
        """Select the target model based on tier and provider preference."""
        provider = self._preferred_provider
        tier_map = self._config.model_map.get(provider, {})
        model = tier_map.get(tier.value)

        if model:
            return model

        # Fallback: return configured default
        return self._config.default_model

    def route(self, model_requested: str, messages: list[dict], override_model: str | None = None) -> RoutingDecision:
        """Route a request to the appropriate model.

        Args:
            model_requested: The model specified in the original request.
            messages: The conversation messages.
            override_model: If set via X-NightJar-Model-Override header,
                          forces this model regardless of classification.

        Returns:
            A RoutingDecision with the selected model and reasoning.
        """
        text = self._flatten_messages(messages)
        token_count = self.count_tokens(text, model_requested)

        # Handle override header
        if override_model:
            logger.info("router.override", override_model=override_model)
            return RoutingDecision(
                tier=Tier.COMPLEX,
                model_requested=model_requested,
                model_selected=override_model,
                reason="header_override",
                token_count=token_count,
                override=True,
            )

        # Classify complexity
        tier, reason = self._classify(text, token_count)
        model_selected = self._select_model(tier, model_requested)

        # TEST_MODE: always use the configured simple model (cheapest tier)
        if self._test_mode:
            simple_model = self._config.model_map.get(self._preferred_provider, {}).get(
                "simple", self._config.default_model
            )
            model_selected = simple_model
            reason = f"test_mode:{reason}"
            logger.info("router.test_mode", forced_model=model_selected)

        logger.info(
            "router.decision",
            tier=tier.value,
            model_requested=model_requested,
            model_selected=model_selected,
            reason=reason,
            token_count=token_count,
        )

        return RoutingDecision(
            tier=tier,
            model_requested=model_requested,
            model_selected=model_selected,
            reason=reason,
            token_count=token_count,
        )

