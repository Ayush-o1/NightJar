"""Rule-based prompt complexity classifier and model router.

Two separate questions are answered here:

1. **Classification**: is this prompt SIMPLE or COMPLEX? (token count + keyword
   rules from config/routing_rules.yaml.) Always computed, because it is cheap and
   useful even when it is not acted on.
2. **Application**: do we actually switch the caller's model to the tier's model?
   That is controlled by ``routing_mode``:

   ============ ==========================================================
   ``explicit`` (default) the caller's model is honoured unless they asked for
                the reserved alias ``nightjar/auto`` or sent the override header
   ``always``   every request is routed (the pre-2.0 behaviour)
   ``off``      never switch models; classification is still recorded
   ============ ==========================================================

Why ``explicit`` is the default: a drop-in OpenAI-compatible endpoint that
silently answers a ``gpt-4o`` request with ``gpt-3.5-turbo`` changes behaviour
callers did not ask for (see docs/TECHNICAL_AUDIT.md, R-1). Because the
classification is still recorded, an operator can look at ``tier`` /
``routing_reason`` in telemetry and see what routing *would* have done before
turning it on.

Evidence caveat: nothing here measures whether the cheaper model's answer was
good enough. The labeled-set accuracy only measures agreement with labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import structlog
import yaml

from app.tokens import content_to_text, count_text_tokens

logger = structlog.get_logger()

# Keywords are only searched in the first part of the prompt. Anything longer is COMPLEX by
# token count anyway, so scanning megabytes could only change the reason string, at real CPU cost.
KEYWORD_SCAN_CHARS = 20_000

ROUTING_ALIAS = "nightjar/auto"
ROUTING_MODES = ("explicit", "always", "off")


class Tier(str, Enum):
    """Complexity tier for routing decisions."""

    SIMPLE = "simple"
    COMPLEX = "complex"


@dataclass
class RoutingDecision:
    """Result of the routing step.

    ``model_selected`` is what will actually be called. ``suggested_model`` is what
    the tier rules would pick; it differs from ``model_selected`` when routing was
    not applied.
    """

    tier: Tier
    model_requested: str
    model_selected: str
    reason: str
    token_count: int
    override: bool = False
    applied: bool = True
    suggested_model: str | None = None


@dataclass
class RoutingConfig:
    """Parsed routing configuration from YAML."""

    simple_max_tokens: int = 200
    complex_min_tokens: int = 500
    complex_keywords: list[str] = field(default_factory=list)
    simple_keywords: list[str] = field(default_factory=list)
    model_map: dict[str, dict[str, str]] = field(default_factory=dict)
    default_model: str = "gpt-3.5-turbo"


def _keyword_pattern(keyword: str, *, prefix: bool = False) -> re.Pattern[str]:
    """Compile a keyword/phrase matcher.

    Whole-word by default, so 'hi' does not match 'which' and 'no' does not match 'know'.
    With ``prefix=True`` only the left edge is anchored, so 'optimize' still matches
    'optimized' and 'analyze' matches 'analyzed'. Complex keywords use prefix matching:
    wrongly sending a hard prompt to the cheap model is the costlier mistake, so for
    them a missed variant is worse than an extra match.
    """
    right = "" if prefix else r"(?!\w)"
    return re.compile(rf"(?<!\w){re.escape(keyword)}{right}")


class ModelRouter:
    """Classifies prompt complexity and selects the appropriate model."""

    def __init__(
        self,
        config_path: str = "config/routing_rules.yaml",
        preferred_provider: str = "openai",
        test_mode: bool = False,
        simple_model: str | None = None,
        complex_model: str | None = None,
        routing_mode: str = "explicit",
    ) -> None:
        if routing_mode not in ROUTING_MODES:
            raise ValueError(f"routing_mode must be one of {ROUTING_MODES}, got {routing_mode!r}")
        self._preferred_provider = preferred_provider
        self._test_mode = test_mode
        self._routing_mode = routing_mode
        self._config = self._load_config(config_path)
        # Runtime overrides take precedence over YAML model_map values
        if simple_model:
            self._config.model_map.setdefault(preferred_provider, {})["simple"] = simple_model
        if complex_model:
            self._config.model_map.setdefault(preferred_provider, {})["complex"] = complex_model
        self._complex_patterns = [(k, _keyword_pattern(k, prefix=True)) for k in self._config.complex_keywords]
        self._simple_patterns = [(k, _keyword_pattern(k)) for k in self._config.simple_keywords]
        logger.info(
            "router.loaded",
            provider=preferred_provider,
            mode=routing_mode,
            simple_max_tokens=self._config.simple_max_tokens,
            complex_keywords_count=len(self._config.complex_keywords),
            simple_model=self._config.model_map.get(preferred_provider, {}).get("simple"),
            complex_model=self._config.model_map.get(preferred_provider, {}).get("complex"),
            test_mode=test_mode,
        )

    @property
    def routing_mode(self) -> str:
        return self._routing_mode

    @staticmethod
    def _load_config(config_path: str) -> RoutingConfig:
        """Load and parse the routing configuration YAML."""
        path = Path(config_path)
        if not path.exists():
            logger.warning("router.config_not_found", path=config_path)
            return RoutingConfig()

        with open(path) as f:
            raw = yaml.safe_load(f) or {}

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
        """Count tokens; exact for ordinary sizes, estimated for very large text (see app/tokens.py)."""
        return count_text_tokens(text, model)

    def _flatten_messages(self, messages: list[dict]) -> str:
        """Flatten messages list into a single text string for analysis."""
        parts = []
        for msg in messages:
            content = content_to_text(msg.get("content"))
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

        Keywords match whole words/phrases only.
        """
        text_lower = text[:KEYWORD_SCAN_CHARS].lower()

        for keyword, pattern in self._complex_patterns:
            if pattern.search(text_lower):
                return Tier.COMPLEX, f"complex_keyword:{keyword}"

        if token_count >= self._config.complex_min_tokens:
            return Tier.COMPLEX, f"token_count:{token_count}>={self._config.complex_min_tokens}"

        if token_count <= self._config.simple_max_tokens:
            for keyword, pattern in self._simple_patterns:
                if pattern.search(text_lower):
                    return Tier.SIMPLE, f"simple_keyword:{keyword}"
            return Tier.SIMPLE, f"token_count:{token_count}<={self._config.simple_max_tokens}"

        return Tier.COMPLEX, "ambiguous_default"

    def _select_model(self, tier: Tier) -> str:
        """Model for a tier under the preferred provider (falls back to the configured default)."""
        tier_map = self._config.model_map.get(self._preferred_provider, {})
        return tier_map.get(tier.value) or self._config.default_model

    def _should_apply(self, model_requested: str) -> bool:
        if self._routing_mode == "always":
            return True
        if self._routing_mode == "off":
            return False
        return model_requested == ROUTING_ALIAS

    def route(self, model_requested: str, messages: list[dict], override_model: str | None = None) -> RoutingDecision:
        """Decide which model serves this request.

        Args:
            model_requested: The model named in the request body (or the alias ``nightjar/auto``).
            messages: The conversation messages.
            override_model: Model from the ``X-NightJar-Model-Override`` header. Forces that
                model regardless of mode and classification.
        """
        text = self._flatten_messages(messages)
        token_count = self.count_tokens(text, model_requested if model_requested != ROUTING_ALIAS else "gpt-4o")

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

        tier, reason = self._classify(text, token_count)
        suggested = self._select_model(tier)
        apply = self._should_apply(model_requested)

        if apply:
            model_selected = suggested
        elif model_requested == ROUTING_ALIAS:  # alias but mode=off: there is no real model to call
            model_selected = suggested
            apply = True
        else:
            model_selected = model_requested

        # TEST_MODE: force the cheapest configured model for every request.
        if self._test_mode:
            model_selected = self._select_model(Tier.SIMPLE)
            reason = f"test_mode:{reason}"
            apply = True
            logger.info("router.test_mode", forced_model=model_selected)

        if not apply:
            reason = f"not_applied({self._routing_mode}):{reason}"

        logger.info(
            "router.decision",
            tier=tier.value,
            mode=self._routing_mode,
            applied=apply,
            model_requested=model_requested,
            model_selected=model_selected,
            suggested_model=suggested,
            reason=reason,
            token_count=token_count,
        )

        return RoutingDecision(
            tier=tier,
            model_requested=model_requested,
            model_selected=model_selected,
            reason=reason,
            token_count=token_count,
            applied=apply,
            suggested_model=suggested,
        )
