"""Benchmark utility functions for NightJar.

Extracted into a separate module so they can be tested by pytest without
requiring a live server.
"""

from __future__ import annotations

import dataclasses
import math
import random
from typing import Any

# ── Paraphrasing ─────────────────────────────────────────────────────────

SYNONYM_MAP: dict[str, list[str]] = {
    "what is": ["tell me about", "describe", "explain what"],
    "explain": ["describe", "elaborate on", "walk me through"],
    "how to": ["what is the way to", "steps to", "how can I"],
    "why": ["what is the reason", "for what reason"],
    "can you": ["would you", "could you", "please"],
    "tell me": ["let me know", "share with me", "inform me about"],
    "list": ["enumerate", "give me a list of", "name"],
    "compare": ["contrast", "differentiate between", "what are the differences between"],
    "define": ["what is the definition of", "what does it mean"],
    "write": ["compose", "draft", "create"],
}


def paraphrase(text: str) -> str:
    """Apply simple synonym swapping to create a paraphrased version.

    At least one substitution is attempted. If no synonym matches, the
    original text is returned with minor word-order shuffling.
    """
    result = text.lower()
    swapped = False

    # Shuffle the synonym map order for variety
    items = list(SYNONYM_MAP.items())
    random.shuffle(items)

    for original, synonyms in items:
        if original in result:
            replacement = random.choice(synonyms)
            result = result.replace(original, replacement, 1)
            swapped = True
            break

    if not swapped:
        # Fallback: add a trailing phrase
        suffixes = [" in detail", " briefly", " for me", " please"]
        result = result.rstrip("?. ") + random.choice(suffixes)

    # Capitalize first letter
    if result:
        result = result[0].upper() + result[1:]

    return result


# ── Latency Statistics ───────────────────────────────────────────────────


@dataclasses.dataclass
class LatencyStats:
    """Computed latency percentiles and extremes."""

    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float

    def to_dict(self) -> dict[str, float]:
        return dataclasses.asdict(self)


def compute_latency_stats(latencies_ms: list[float]) -> LatencyStats:
    """Compute p50, p95, p99, min, max from a list of latencies in ms."""
    if not latencies_ms:
        return LatencyStats(p50_ms=0, p95_ms=0, p99_ms=0, min_ms=0, max_ms=0)

    sorted_lat = sorted(latencies_ms)
    n = len(sorted_lat)

    def percentile(p: float) -> float:
        idx = (p / 100.0) * (n - 1)
        lower = int(math.floor(idx))
        upper = min(lower + 1, n - 1)
        frac = idx - lower
        return sorted_lat[lower] * (1 - frac) + sorted_lat[upper] * frac

    return LatencyStats(
        p50_ms=round(percentile(50), 2),
        p95_ms=round(percentile(95), 2),
        p99_ms=round(percentile(99), 2),
        min_ms=round(sorted_lat[0], 2),
        max_ms=round(sorted_lat[-1], 2),
    )


# ── Routing Accuracy ─────────────────────────────────────────────────────


@dataclasses.dataclass
class RoutingAccuracy:
    """Routing accuracy result with confusion matrix."""

    accuracy: float
    total: int
    correct: int
    confusion_matrix: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def compute_routing_accuracy(
    predictions: list[str], labels: list[str]
) -> RoutingAccuracy:
    """Compute routing accuracy and confusion matrix.

    Args:
        predictions: list of predicted tiers (e.g. ``"simple"``, ``"complex"``).
        labels: list of ground-truth labels (same length as *predictions*).

    Returns:
        RoutingAccuracy with accuracy, total, correct, and confusion matrix
        (TP=simple-correct, TN=complex-correct, FP=predicted-complex-was-simple,
         FN=predicted-simple-was-complex).
    """
    if len(predictions) != len(labels):
        raise ValueError("predictions and labels must have the same length")

    correct = 0
    tp = 0  # True positive: predicted simple, actually simple
    tn = 0  # True negative: predicted complex, actually complex
    fp = 0  # False positive: predicted complex, actually simple
    fn = 0  # False negative: predicted simple, actually complex

    for pred, label in zip(predictions, labels):
        pred_lower = pred.lower()
        label_lower = label.lower()
        if pred_lower == label_lower:
            correct += 1
            if label_lower == "simple":
                tp += 1
            else:
                tn += 1
        else:
            if pred_lower == "complex" and label_lower == "simple":
                fp += 1
            else:
                fn += 1

    total = len(labels)
    accuracy = correct / total if total > 0 else 0.0

    return RoutingAccuracy(
        accuracy=round(accuracy, 4),
        total=total,
        correct=correct,
        confusion_matrix={"TP": tp, "TN": tn, "FP": fp, "FN": fn},
    )


# ── Benchmark Result Container ───────────────────────────────────────────


@dataclasses.dataclass
class BenchmarkResult:
    """Complete benchmark run result, JSON-serializable."""

    timestamp: str
    cache_hit_rate: float = 0.0
    avg_latency_miss_ms: float = 0.0
    avg_latency_hit_ms: float = 0.0
    speedup_factor: float = 0.0
    routing_accuracy: float = 0.0
    routing_confusion_matrix: dict[str, int] = dataclasses.field(default_factory=dict)
    latency_stats: dict[str, float] = dataclasses.field(default_factory=dict)
    assertions_passed: bool = True
    assertion_failures: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
