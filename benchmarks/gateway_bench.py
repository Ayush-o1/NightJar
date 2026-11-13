#!/usr/bin/env python
"""Measure what NightJar adds to a request, and what a cache hit saves, against a fake provider.

    uv run python benchmarks/gateway_bench.py                # full run (loads the embedding model once)
    uv run python benchmarks/gateway_bench.py --smoke        # ~10 s sanity run, no embedding model

Processes started (and stopped) by this script: a fake OpenAI-compatible provider with a fixed latency,
a throwaway Redis, and NightJar under uvicorn (one worker, auth off). The load generator is httpx in this
process. Nothing leaves the machine and no API key is used.

What it can and cannot say: it measures the gateway's own overhead and cache-hit latency against a provider
whose latency it controls. It does **not** measure real provider latency or real savings, which need real
provider credentials. See docs/benchmarks/GATEWAY_OVERHEAD.md for the method and limits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 120) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


async def run_load(url: str, make_body, total: int, concurrency: int, headers: dict | None = None) -> dict:
    """Send ``total`` requests with ``concurrency`` in flight; body i comes from make_body(i)."""
    latencies: list[float] = []
    errors = 0
    counter = iter(range(total))
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=60) as client:

        async def worker():
            nonlocal errors
            for i in counter:
                t = time.perf_counter()
                try:
                    r = await client.post(url, json=make_body(i), headers=headers)
                    ok = r.status_code == 200
                except httpx.HTTPError:
                    ok = False
                latencies.append((time.perf_counter() - t) * 1000)
                errors += 0 if ok else 1

        start = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(concurrency)])
        elapsed = time.perf_counter() - start
    return {
        "n": total, "concurrency": concurrency, "errors": errors, "error_rate": errors / total,
        "throughput_rps": round(total / elapsed, 1),
        "p50_ms": round(pct(latencies, 0.50), 2), "p95_ms": round(pct(latencies, 0.95), 2),
        "p99_ms": round(pct(latencies, 0.99), 2), "mean_ms": round(sum(latencies) / len(latencies), 2),
    }


def chat_body(text: str, **extra) -> dict:
    return {"model": "gpt-4o", "temperature": 0, "messages": [{"role": "user", "content": text}], **extra}


class Stack:
    """Fake provider + Redis + one NightJar process, torn down on exit."""

    def __init__(self, provider_latency_ms: float, semantic: bool, tmp: Path) -> None:
        self.procs: list[subprocess.Popen] = []
        self.latency = provider_latency_ms
        self.semantic = semantic
        self.tmp = tmp

    def __enter__(self):
        py = sys.executable
        self.fake_port, self.redis_port, self.gw_port = free_port(), free_port(), free_port()
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        self.procs.append(subprocess.Popen(
            [py, str(ROOT / "benchmarks/fake_upstream.py"), "--port", str(self.fake_port),
             "--latency-ms", str(self.latency)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        redis_bin = shutil.which("redis-server")
        if not redis_bin:
            raise SystemExit("redis-server not found on PATH (needed for the cache)")
        self.procs.append(subprocess.Popen(
            [redis_bin, "--port", str(self.redis_port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        gw_env = {
            **env, "OPENAI_API_KEY": "sk-bench", "OPENAI_API_BASE": f"http://127.0.0.1:{self.fake_port}/v1",
            "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}", "NIGHTJAR_AUTH": "off",
            "SQLITE_DB_PATH": str(self.tmp / f"bench-{self.gw_port}.db"),
            "VECTOR_INDEX_PATH": str(self.tmp / f"vec-{self.gw_port}.npz"),
            "SEMANTIC_CACHE": "true" if self.semantic else "false", "LOG_LEVEL": "WARNING",
            "MAX_CONCURRENT_UPSTREAM": "512",
        }
        self.gw = subprocess.Popen(
            [py, "-m", "uvicorn", "app.main:app", "--port", str(self.gw_port), "--log-level", "warning"],
            cwd=ROOT, env=gw_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(self.gw)
        wait_http(f"http://127.0.0.1:{self.fake_port}/_stats")
        wait_http(f"http://127.0.0.1:{self.gw_port}/health", timeout=180)
        return self

    def __exit__(self, *exc):
        for p in reversed(self.procs):
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    @property
    def chat_url(self) -> str:
        return f"http://127.0.0.1:{self.gw_port}/v1/chat/completions"

    @property
    def direct_url(self) -> str:
        return f"http://127.0.0.1:{self.fake_port}/v1/chat/completions"

    def provider_calls(self) -> int:
        return httpx.get(f"http://127.0.0.1:{self.fake_port}/_stats").json()["requests"]


def scenario(label: str, result: dict, extra: dict | None = None) -> dict:
    row = {"scenario": label, **result, **(extra or {})}
    print(f"  {label:<44} p50 {row['p50_ms']:>8.1f}  p95 {row['p95_ms']:>8.1f}  p99 {row['p99_ms']:>8.1f} ms | "
          f"{row['throughput_rps']:>7.1f} rps | errors {row['errors']}", flush=True)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="short run, skips the semantic tier")
    ap.add_argument("--provider-latency-ms", type=float, default=50.0)
    ap.add_argument("--json", type=Path, help="write results as JSON")
    args = ap.parse_args()

    n = 60 if args.smoke else 400
    levels = [1, 16] if args.smoke else [1, 16, 64]
    tmp = Path(tempfile.mkdtemp(prefix="nightjar-bench-"))
    results: list[dict] = []
    env_info = {
        "machine": platform.machine(), "platform": platform.platform(), "python": platform.python_version(),
        "cpu": subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
        ).stdout.strip(),
        "cores": os.cpu_count(), "provider_latency_ms": args.provider_latency_ms, "uvicorn_workers": 1,
        "requests_per_scenario": n, "date": time.strftime("%Y-%m-%d"),
    }
    print(f"fake provider latency {args.provider_latency_ms} ms; {n} requests per scenario; env: {env_info['cpu']}")

    try:
        with Stack(args.provider_latency_ms, semantic=False, tmp=tmp) as st:
            print("\nsemantic tier OFF (exact tier only)")
            asyncio.run(run_load(st.chat_url, lambda i: chat_body(f"warmup {i}"), 20, 4,
                                 {"X-NightJar-Cache": "bypass"}))
            results_rss = {"gateway_rss_mb_start": round(rss_mb(st.gw.pid), 1)}
            for c in levels:
                results.append(scenario(f"direct to provider, c={c}", asyncio.run(
                    run_load(st.direct_url, lambda i: chat_body(f"direct {i}"), n, c)), {"group": "baseline"}))
                results.append(scenario(f"NightJar miss path (cache bypass), c={c}", asyncio.run(
                    run_load(st.chat_url, lambda i: chat_body(f"miss {c} {i}"), n, c, {"X-NightJar-Cache": "bypass"})),
                    {"group": "miss"}))
                asyncio.run(run_load(st.chat_url, lambda i: chat_body("the one hot prompt"), 1, 1))
                calls_before = st.provider_calls()
                row = scenario(f"NightJar exact-cache hit, c={c}", asyncio.run(
                    run_load(st.chat_url, lambda i: chat_body("the one hot prompt"), n, c)), {"group": "hit"})
                row["provider_calls_during"] = st.provider_calls() - calls_before
                results.append(row)
            results_rss["gateway_rss_mb_end"] = round(rss_mb(st.gw.pid), 1)
            env_info.update(results_rss)

        if not args.smoke:
            with Stack(args.provider_latency_ms, semantic=True, tmp=tmp) as st:
                print("\nsemantic tier ON (loads all-MiniLM-L6-v2)")
                asyncio.run(run_load(st.chat_url, lambda i: chat_body(f"warmup {i}"), 20, 4))
                for c in (1, 16):
                    def unique(i, c=c):
                        return chat_body(f"unique question {c}-{i} about topic {i % 7}")

                    results.append(scenario(
                        f"semantic ON, unique prompts (miss + embed + store), c={c}",
                        asyncio.run(run_load(st.chat_url, unique, n, c)), {"group": "semantic_miss"}))
                asyncio.run(run_load(st.chat_url, lambda i: chat_body("How do I reset my password?"), 1, 1))
                results.append(scenario("semantic ON, paraphrase hit (embed + search), c=1", asyncio.run(
                    run_load(st.chat_url, lambda i: chat_body("How can I reset my password?"), 100, 1)),
                    {"group": "semantic_hit"}))
                env_info["gateway_rss_mb_semantic"] = round(rss_mb(st.gw.pid), 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out = {"environment": env_info, "results": results}
    if args.json:
        args.json.write_text(json.dumps(out, indent=2))
    bad = [r for r in results if r["error_rate"] > 0.01]
    if bad:
        print(f"\nFAILED: error rate above 1% in {[r['scenario'] for r in bad]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
