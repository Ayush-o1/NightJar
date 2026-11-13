# Baseline

State of the repository before any modernization work. Everything here was measured or observed on 2026-11-13 against commit `1a4819c` (`main`, 58 commits, tags v0.1.0–v1.0.0). Nothing in the original code was changed to produce these numbers.

## Environment

| | |
|---|---|
| Machine | macOS (Apple Silicon), Darwin 27.2 |
| Python | 3.11.18 (via `uv venv`, same minor version as CI and the Dockerfile) |
| Dependencies | `uv pip install -r requirements.txt`, unmodified |
| Resolved versions | litellm 1.95.1, fastapi 0.115.6, starlette 0.41.3, faiss-cpu 1.16.0, sentence-transformers 3.3.1, torch 2.15.0, numpy 2.4.6, redis-py 5.2.1 |
| Redis | 8.10.3 (Homebrew) for live probes. `docker-compose.yml` uses `redis:7-alpine`, so this differs. |
| Docker | Client installed, **daemon not running**. The image was not built, so Docker is untested in this baseline. |

`requirements.txt` pins most packages exactly but leaves `litellm>=1.30.0` open. A fresh install therefore resolved litellm 1.95.1, a version 65 minor releases past the floor. The suite passes on it, but the build is not reproducible.

## Checks

| Check | Result |
|---|---|
| `ruff check app/ tests/ benchmarks/` | Pass, no findings |
| `pytest tests/` (exactly as CI runs it) | **175 passed, 0 failed, 0 skipped, 22 deselected**, 1.08 s |
| Deselected tests | `tests/test_e2e.py`, marker `e2e`, needs a real provider key (`RUN_E2E_TESTS=1`). Not run. |
| Type checking | None configured |
| App startup (`uvicorn app.main:app`, real Redis, real embedder) | Starts. `/health` returns `{"status":"ok","version":"1.0.0","redis":"ok"}` |
| Docker build | Not run (no daemon) |
| Benchmark `--dry-run` | Not run directly. Read the code instead, see below. |

No test failed before my changes, so there is no pre-existing failure to attribute.

### What the passing suite does and doesn't cover

All 175 tests replace `proxy_client`, `cache`, `router` and `settings` on `app.state` with mocks (`tests/conftest.py`). So they test each module in isolation, but nothing runs the real pipeline with a real Redis, FAISS index and embedder in the same process. The only test that does is `test_e2e.py`, which needs a live paid provider and is excluded.

A side effect: a session fixture calls `tel.init_db()` against the real `./data/telemetry.db`, so running the tests creates and writes a `data/` directory in the working tree.

## Measurements

### Routing accuracy

README says "Measured at ~88% accuracy against a labeled 1,000-prompt set."

`benchmarks/run_benchmark.py --dry-run` (the mode CI runs) never calls the router. It generates `random.random() < 0.88` with `random.seed(42)`. The 88% in the README matches that constant, so I treat it as unmeasured.

I ran the actual `ModelRouter` over `benchmarks/prompts_labeled.json`:

| | predicted simple | predicted complex |
|---|---|---|
| **labeled simple (500)** | 436 | 64 |
| **labeled complex (500)** | 8 | 492 |

Accuracy **92.8%**. All 64 simple→complex errors are the keyword `summarize` sending short "Summarize: …" prompts to the complex tier.

Caveat on what this number means: the dataset has 1,000 prompts with 999 unique, a median of 7 words and a maximum of 13. None is long enough to reach the 200/500-token rules. The labels line up with the router's own keyword list ("Debug…", "Implement…", "Write code…"). So 92.8% says the keyword list matches the labels that were written alongside it. It says nothing about how well routing predicts whether a cheap model would have answered correctly.

### Semantic cache: false hits with the shipped model and threshold

Cosine similarity from `all-MiniLM-L6-v2` (the shipped embedder) for pairs where the answers differ. Default threshold is 0.92. The adaptive controller can lower it to 0.70.

| similarity | same answer? | pair |
|---|---|---|
| 0.992 | no | "Convert 10 miles to kilometers" / "Convert 10 kilometers to miles" |
| 0.951 | no | "…sorts a list ascending" / "…descending" |
| 0.944 | no | "What was the revenue in 2022?" / "…2023?" |
| 0.923 | yes | "What is the capital of France?" / "Tell me the capital city of France" |
| 0.860 | yes | "How do I reset my password?" / "How can I change my password?" |
| 0.846 | no | "Translate 'good morning' to French" / "…Spanish" |
| 0.770 | no | ibuprofen / acetaminophen with alcohol |
| 0.702 | no | "reset my password" / "reset my username" |

Pairs with different answers score higher than a genuine paraphrase. No threshold separates them: 0.92 serves the miles/km and 2022/2023 pairs wrongly and still misses the password paraphrase. This is a property of the model, not of the threshold value. Ten hand-written pairs is a probe, not an evaluation, and Phase 5 builds a proper one.

The cache also never sees the request parameters. `cache.lookup` is called with `(model, messages, threshold)` only, and `model` is the client's requested name, not the routed one. Temperature, `max_tokens`, `tools`, `response_format`, `n` and the system prompt (which is mixed into the embedded text) do not distinguish entries. The FAISS search is global, with no filter on model.

### Event-loop blocking from large bodies

There is no request size limit. Sending one 10 MB request (`"a" × 10,000,000` in a message) pinned the server at 100% CPU. `/health` did not answer for more than 240 seconds, and SIGTERM did not stop the process, so I killed it with SIGKILL. Cause, reproduced in isolation: the router calls `tiktoken` synchronously on the event loop, and tiktoken is quadratic on long runs without whitespace.

| input | tiktoken encode time |
|---|---|
| 10,000 × `a` | 0.019 s |
| 50,000 × `a` | 0.498 s |
| 100,000 × `a` | 2.037 s |
| 200,000 × `a` | 8.596 s |
| 1,000,000 chars of normal text | 0.030 s |

Ordinary prose is fine. One unauthenticated (auth is off by default) request of a few hundred KB is enough to stall every other client.

### Failure behavior observed

| Case | Observed |
|---|---|
| Message `content` as a list of parts (standard OpenAI multimodal and text-part shape) | `422`, FastAPI-style `{"detail":[…]}`. `ChatMessage.content` is `str \| None`. |
| Malformed JSON | `422`, FastAPI `detail` format, not an OpenAI-style `error` object |
| Empty `messages: []` | Accepted and forwarded |
| Unexpected exception in the pipeline | `500` with the plain-text body `Internal Server Error` |
| Streaming request, upstream fails | `UpstreamError` is raised inside the response generator after headers were sent, giving an unhandled-exception traceback in the log and a truncated stream |
| One rejected request (invalid key) to the only deployment of a model group | LiteLLM put that deployment into cooldown: the next request failed with "No deployments available… Try again in 5 seconds" |
| Cache hit telemetry | `prompt_tokens=0, completion_tokens=0, cost=0`, so `/admin/savings` `cache_savings_usd` is computed from those zeros and is always `0.0` |
| Auth with `CONTEXTFORGE_API_KEYS` unset (default) | `DELETE /v1/cache` returned 200 to an anonymous caller. `/admin/usage` was readable too. |

### Not measured

- **Latency and throughput against a direct provider call.** No provider key was available and I did not want to spend the owner's money or send traffic to a paid API for a baseline. Phase 11 will compare against a local fake upstream with controlled latency instead, and will say so.
- **Resource use under concurrent load.**
- **Docker image build, size and health check.**
- **Live E2E tests.**
