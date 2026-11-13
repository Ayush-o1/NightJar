#!/usr/bin/env python
"""End-to-end benchmark runner for NightJar.

Usage:
    # Full benchmark (requires live NightJar + Redis):
    python benchmarks/run_benchmark.py

    # Dry-run (no live server, uses fixture data — for CI):
    python benchmarks/run_benchmark.py --dry-run

Exit code 0 if all assertions pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import sys
import time
from pathlib import Path

# Ensure the repo root is on sys.path so ``benchmarks.benchmark_utils`` works
# when the script is invoked as ``python benchmarks/run_benchmark.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from benchmarks.benchmark_utils import (  # noqa: E402
    BenchmarkResult,
    compute_latency_stats,
    compute_routing_accuracy,
    paraphrase,
)

# ── Configuration ────────────────────────────────────────────────────────

BASE_URL = os.getenv("NIGHTJAR_URL") or os.getenv("CONTEXTFORGE_URL") or "http://localhost:8000"
PROMPTS_PATH = Path(__file__).parent / "prompts_labeled.json"
RESULTS_DIR = Path(__file__).parent / "results"

CACHE_BENCH_SIZE = 50
LATENCY_BENCH_SIZE = 100

# Assertion thresholds
MIN_ROUTING_ACCURACY = 0.85
MAX_P95_MS = 5000
MIN_CACHE_HIT_RATE = 0.40

# ── Pretty printing ─────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.table import Table

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


def _print_report(result: BenchmarkResult) -> None:
    """Print benchmark results to stdout."""
    if _HAS_RICH:
        _print_rich(result)
    else:
        _print_plain(result)


def _print_rich(result: BenchmarkResult) -> None:
    console = Console()
    console.print("\n[bold cyan]═══ NightJar Benchmark Report ═══[/bold cyan]\n")

    # Cache
    t1 = Table(title="Cache Hit Rate Benchmark")
    t1.add_column("Metric", style="bold")
    t1.add_column("Value")
    t1.add_row("Cache Hit Rate", f"{result.cache_hit_rate:.2%}")
    t1.add_row("Avg Latency (miss)", f"{result.avg_latency_miss_ms:.1f} ms")
    t1.add_row("Avg Latency (hit)", f"{result.avg_latency_hit_ms:.1f} ms")
    t1.add_row("Speedup Factor", f"{result.speedup_factor:.1f}x")
    console.print(t1)

    # Routing
    t2 = Table(title="Routing Accuracy Benchmark")
    t2.add_column("Metric", style="bold")
    t2.add_column("Value")
    t2.add_row("Accuracy", f"{result.routing_accuracy:.2%}")
    for k, v in result.routing_confusion_matrix.items():
        t2.add_row(f"  {k}", str(v))
    console.print(t2)

    # Latency
    t3 = Table(title="Latency Benchmark")
    t3.add_column("Percentile", style="bold")
    t3.add_column("ms")
    for k, v in result.latency_stats.items():
        t3.add_row(k, f"{v:.1f}")
    console.print(t3)

    # Assertions
    if result.assertions_passed:
        console.print("\n[bold green]✅ All assertions passed[/bold green]")
    else:
        console.print("\n[bold red]❌ Assertion failures:[/bold red]")
        for f in result.assertion_failures:
            console.print(f"  • {f}", style="red")


def _print_plain(result: BenchmarkResult) -> None:
    print("\n═══ NightJar Benchmark Report ═══\n")
    print("--- Cache Hit Rate ---")
    print(f"  Cache Hit Rate:      {result.cache_hit_rate:.2%}")
    print(f"  Avg Latency (miss):  {result.avg_latency_miss_ms:.1f} ms")
    print(f"  Avg Latency (hit):   {result.avg_latency_hit_ms:.1f} ms")
    print(f"  Speedup Factor:      {result.speedup_factor:.1f}x")
    print("\n--- Routing Accuracy ---")
    print(f"  Accuracy: {result.routing_accuracy:.2%}")
    for k, v in result.routing_confusion_matrix.items():
        print(f"    {k}: {v}")
    print("\n--- Latency ---")
    for k, v in result.latency_stats.items():
        print(f"  {k}: {v:.1f} ms")
    print()
    if result.assertions_passed:
        print("✅ All assertions passed")
    else:
        print("❌ Assertion failures:")
        for f in result.assertion_failures:
            print(f"  • {f}")


# ── Live Benchmark Functions ─────────────────────────────────────────────


def _load_prompts() -> list[dict]:
    return json.loads(PROMPTS_PATH.read_text())


def _send_request(prompt: str, model: str = "gpt-3.5-turbo", headers: dict | None = None) -> tuple[dict, float]:
    """Send a chat completion request. Returns (headers_dict, latency_ms)."""
    import httpx

    url = f"{BASE_URL}/v1/chat/completions"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    start = time.perf_counter()
    resp = httpx.post(url, json=body, headers=req_headers, timeout=30.0)
    latency_ms = (time.perf_counter() - start) * 1000

    resp_headers = dict(resp.headers)
    return resp_headers, latency_ms


def _run_cache_benchmark(prompts: list[dict]) -> dict:
    """Run cache hit rate benchmark on first CACHE_BENCH_SIZE prompts."""
    selected = prompts[:CACHE_BENCH_SIZE]

    miss_latencies = []
    hit_latencies = []
    hits = 0

    # Phase 1: send originals (expect cache misses)
    for p in selected:
        _, latency = _send_request(p["prompt"])
        miss_latencies.append(latency)

    # Phase 2: replay with paraphrasing (expect some cache hits)
    for p in selected:
        paraphrased = paraphrase(p["prompt"])
        resp_headers, latency = _send_request(paraphrased)
        cache_hit = resp_headers.get("x-cache", "").upper() == "HIT"
        if cache_hit:
            hits += 1
            hit_latencies.append(latency)
        else:
            miss_latencies.append(latency)

    total_replays = len(selected)
    cache_hit_rate = hits / total_replays if total_replays else 0.0
    avg_miss = sum(miss_latencies) / len(miss_latencies) if miss_latencies else 0.0
    avg_hit = sum(hit_latencies) / len(hit_latencies) if hit_latencies else 0.0
    speedup = avg_miss / avg_hit if avg_hit > 0 else 0.0

    return {
        "cache_hit_rate": round(cache_hit_rate, 4),
        "avg_latency_miss_ms": round(avg_miss, 2),
        "avg_latency_hit_ms": round(avg_hit, 2),
        "speedup_factor": round(speedup, 1),
    }


def _run_routing_benchmark(prompts: list[dict]) -> dict:
    """Run routing accuracy benchmark on all prompts."""
    predictions = []
    labels = []

    for p in prompts:
        resp_headers, _ = _send_request(p["prompt"])
        tier = resp_headers.get("x-model-tier", "simple")
        predictions.append(tier)
        labels.append(p["expected_model_tier"].lower())

    result = compute_routing_accuracy(predictions, labels)
    return result.to_dict()


def _run_latency_benchmark(prompts: list[dict]) -> dict:
    """Run latency benchmark on LATENCY_BENCH_SIZE prompts."""
    selected = prompts[:LATENCY_BENCH_SIZE]
    latencies = []
    for p in selected:
        _, latency = _send_request(p["prompt"])
        latencies.append(latency)

    stats = compute_latency_stats(latencies)
    return stats.to_dict()


# ── Dry-Run Mode ─────────────────────────────────────────────────────────


def _run_dry_run() -> BenchmarkResult:
    """Run benchmark with synthetic fixture data (no live server)."""
    random.seed(42)

    # Simulate cache benchmark
    cache_hit_rate = 0.52
    avg_miss = 450.0
    avg_hit = 25.0
    speedup = avg_miss / avg_hit

    # Simulate routing benchmark
    prompts = _load_prompts()
    labels = [p["expected_model_tier"].lower() for p in prompts]
    # Simulate ~88% accuracy
    predictions = []
    for label in labels:
        if random.random() < 0.88:
            predictions.append(label)
        else:
            predictions.append("complex" if label == "simple" else "simple")

    routing = compute_routing_accuracy(predictions, labels)

    # Simulate latency benchmark
    latencies = [random.uniform(10, 800) for _ in range(100)]
    lat_stats = compute_latency_stats(latencies)

    result = BenchmarkResult(
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        cache_hit_rate=cache_hit_rate,
        avg_latency_miss_ms=avg_miss,
        avg_latency_hit_ms=avg_hit,
        speedup_factor=round(speedup, 1),
        routing_accuracy=routing.accuracy,
        routing_confusion_matrix=routing.confusion_matrix,
        latency_stats=lat_stats.to_dict(),
    )

    # Run assertions
    _check_assertions(result)
    return result


def _run_live() -> BenchmarkResult:
    """Run full benchmark against a live NightJar server."""
    prompts = _load_prompts()

    print("Running cache hit rate benchmark...")
    cache_result = _run_cache_benchmark(prompts)

    print("Running routing accuracy benchmark...")
    routing_result = _run_routing_benchmark(prompts)

    print("Running latency benchmark...")
    latency_result = _run_latency_benchmark(prompts)

    result = BenchmarkResult(
        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        cache_hit_rate=cache_result["cache_hit_rate"],
        avg_latency_miss_ms=cache_result["avg_latency_miss_ms"],
        avg_latency_hit_ms=cache_result["avg_latency_hit_ms"],
        speedup_factor=cache_result["speedup_factor"],
        routing_accuracy=routing_result["accuracy"],
        routing_confusion_matrix=routing_result["confusion_matrix"],
        latency_stats=latency_result,
    )

    _check_assertions(result)
    return result


# ── Assertions ───────────────────────────────────────────────────────────


def _check_assertions(result: BenchmarkResult) -> None:
    """Check hard failure assertions and update the result."""
    failures = []

    if result.routing_accuracy < MIN_ROUTING_ACCURACY:
        failures.append(
            f"routing_accuracy={result.routing_accuracy:.4f} < {MIN_ROUTING_ACCURACY}"
        )

    p95 = result.latency_stats.get("p95_ms", 0)
    if p95 > MAX_P95_MS:
        failures.append(f"p95_ms={p95:.1f} > {MAX_P95_MS}")

    if result.cache_hit_rate < MIN_CACHE_HIT_RATE:
        failures.append(
            f"cache_hit_rate={result.cache_hit_rate:.4f} < {MIN_CACHE_HIT_RATE}"
        )

    result.assertion_failures = failures
    result.assertions_passed = len(failures) == 0


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="NightJar E2E Benchmark Runner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run with synthetic fixture data (no live server required)",
    )
    args = parser.parse_args()

    if args.dry_run:
        print("🔧 Running in dry-run mode (no live server)...")
        result = _run_dry_run()
    else:
        print(f"🚀 Running live benchmark against {BASE_URL}...")
        result = _run_live()

    # Print report
    _print_report(result)

    # Save results to JSON
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"benchmark_{ts}.json"
    out_path.write_text(json.dumps(result.to_dict(), indent=2))
    print(f"\n📄 Results saved to {out_path}")

    return 0 if result.assertions_passed else 1


if __name__ == "__main__":
    sys.exit(main())
