# NightJar Benchmarks

End-to-end performance benchmarks for the NightJar LLM proxy middleware.

## Quick Start

### Full Benchmark (requires live server)

```bash
# 1. Start NightJar + Redis
docker compose up -d

# 2. Run benchmarks
python benchmarks/run_benchmark.py
```

### Dry-Run Mode (no server required — for CI)

```bash
python benchmarks/run_benchmark.py --dry-run
```

Dry-run mode uses synthetic fixture data to verify the benchmark script runs correctly and produces valid JSON output. No live server or Redis required.

---

## What Each Benchmark Measures

### Cache Hit Rate Benchmark

- Sends 50 unique prompts from `prompts_labeled.json`
- Replays each prompt with synonym-based paraphrasing (e.g. "what is" → "tell me about")
- Measures how many paraphrased replays are served from the semantic cache
- Reports: `cache_hit_rate`, `avg_latency_miss_ms`, `avg_latency_hit_ms`, `speedup_factor`

### Routing Accuracy Benchmark

- Sends all 1000 prompts from the labeled dataset
- Reads the `X-Model-Tier` response header (simple/complex)
- Compares to the ground-truth `expected_model_tier` label
- Reports: `routing_accuracy` (must be ≥ 85%), confusion matrix (TP, TN, FP, FN)

### Latency Benchmark

- Sends 100 requests (mix of cache hits and misses)
- Measures client-side latency with `time.perf_counter()`
- Reports: `p50_ms`, `p95_ms`, `p99_ms`, `min_ms`, `max_ms`

---

## Assertions (Hard Failures)

The benchmark enforces these minimum thresholds. If any fail, the script exits with code 1:

| Metric | Threshold | Meaning |
|--------|-----------|---------|
| `routing_accuracy` | ≥ 0.85 | At least 85% of prompts routed to the correct tier |
| `p95_ms` | ≤ 5000 | 95th percentile latency under 5 seconds |
| `cache_hit_rate` | ≥ 0.40 | At least 40% of paraphrased replays should cache-hit |

---

## Interpreting Results

### Output

Results are printed to stdout in a formatted table (uses `rich` if installed, otherwise plain text).

Results are also saved to JSON:
```
benchmarks/results/benchmark_YYYYMMDD_HHMMSS.json
```

### Example JSON Output

```json
{
  "timestamp": "2026-03-27T12:00:00",
  "cache_hit_rate": 0.52,
  "avg_latency_miss_ms": 450.0,
  "avg_latency_hit_ms": 25.0,
  "speedup_factor": 18.0,
  "routing_accuracy": 0.88,
  "routing_confusion_matrix": {"TP": 440, "TN": 440, "FP": 60, "FN": 60},
  "latency_stats": {"p50_ms": 100.0, "p95_ms": 500.0, "p99_ms": 800.0, "min_ms": 10.0, "max_ms": 1200.0},
  "assertions_passed": true,
  "assertion_failures": []
}
```

### Key Metrics

- **Speedup Factor**: How many times faster cache hits are vs misses. Higher is better (typically 10–20x).
- **Routing Accuracy**: Percentage of prompts correctly classified as simple or complex.
- **p95 Latency**: 95% of requests complete within this time — the key SLA metric.

---

## Prerequisites

For the **full benchmark** (not dry-run):
- A running NightJar instance (default: `http://localhost:8000`)
- Redis running (default: `redis://localhost:6379`)
- A provider API key configured. (Phase 11 replaces this with a local fake upstream so no key is needed.)

Set `NIGHTJAR_URL` env var to override the default base URL:
```bash
NIGHTJAR_URL=http://my-server:8000 python benchmarks/run_benchmark.py
```

---

## Flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Run with synthetic fixture data (no live server required) |
