#!/usr/bin/env python
"""A fake OpenAI-compatible provider for tests and benchmarks. Costs nothing, leaves the machine never.

    uv run python benchmarks/fake_upstream.py --port 9100 --latency-ms 200

Point NightJar at it with OPENAI_API_BASE=http://127.0.0.1:9100/v1 and any OPENAI_API_KEY.

Behaviour is controllable per request with headers or per process with flags:
  --latency-ms N        fixed delay before answering (simulates provider latency)
  --fail-rate F         fraction of requests answered with --fail-status (default 500)
  --fail-status N
A message containing the text "[fail:STATUS]" makes that single request fail with STATUS.
GET /_stats returns request counts (how many calls actually reached the "provider").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
CONFIG = {"latency_ms": 0.0, "fail_rate": 0.0, "fail_status": 500}
STATS = {"requests": 0, "stream_requests": 0, "failures": 0}


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


@app.get("/_stats")
async def stats():
    return STATS


@app.post("/_reset")
async def reset():
    for k in STATS:
        STATS[k] = 0
    return STATS


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    STATS["requests"] += 1
    last = _text(body["messages"][-1].get("content"))

    if CONFIG["latency_ms"]:
        await asyncio.sleep(CONFIG["latency_ms"] / 1000)

    forced = re.search(r"\[fail:(\d{3})\]", last)
    if forced or random.random() < CONFIG["fail_rate"]:
        STATS["failures"] += 1
        status = int(forced.group(1)) if forced else CONFIG["fail_status"]
        return JSONResponse(
            {"error": {"message": f"fake upstream failure {status}", "type": "fake_error", "code": str(status)}},
            status_code=status,
        )

    answer = f"answer to: {last[:200]}"
    usage = {"prompt_tokens": max(1, len(last) // 4), "completion_tokens": max(1, len(answer) // 4)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    base = {"id": f"chatcmpl-fake-{STATS['requests']}", "created": int(time.time()), "model": body["model"]}

    if body.get("stream"):
        STATS["stream_requests"] += 1

        async def events():
            words = answer.split(" ")
            for i, w in enumerate(words):
                chunk = {**base, "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.01)
            final = {**base, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(final)}\n\n"
            if (body.get("stream_options") or {}).get("include_usage"):
                usage_chunk = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
                yield f"data: {json.dumps(usage_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return {**base, "object": "chat.completion", "usage": usage,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": answer}}]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--latency-ms", type=float, default=0)
    ap.add_argument("--fail-rate", type=float, default=0)
    ap.add_argument("--fail-status", type=int, default=500)
    a = ap.parse_args()
    CONFIG.update(latency_ms=a.latency_ms, fail_rate=a.fail_rate, fail_status=a.fail_status)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
