# Implementation status

Branch: `feature/nightjar-modernization` (from `main` @ `1a4819c`). Not merged to `main`.

| Phase | Status | Notes |
|---|---|---|
| 0 Discovery and baseline | Done | [BASELINE.md](BASELINE.md), [TECHNICAL_AUDIT.md](TECHNICAL_AUDIT.md) |
| 1 Product definition and research | Done | `docs/research/`, [ROADMAP.md](ROADMAP.md) |
| 2 Rename and foundations | Done | Rename with compatibility aliases; `pyproject.toml` + `uv.lock` + generated `requirements.txt`; single version source; unused deps and `OPENAI_BASE_URL` removed. 187 tests pass (175 original + 12 new). |
| 3 Core API and request lifecycle | Done | Pure-ASGI request context (IDs, 413 limits, 500 safety net); OpenAI-shaped errors everywhere; multimodal `content`; bounded tokenization; in-band streaming errors; telemetry latency now covers streams. 244 tests pass (175 original, 69 new). |
| 4 Routing and provider layer | Partly done | Routing is now opt-in (`explicit` default; alias `nightjar/auto`; `always`/`off`), shadow decisions recorded, whole-word/prefix keyword matching, scan bounded, `ALLOWED_MODELS` allow-list. 276 tests pass. **Moved to Phase 7:** config-driven deployments/fallback chains and the `Router.model_list` deprecation, since they are the same code as the reliability work. |
| 5 Cache correctness | Done | Exact tier + partitioned, guarded, opt-in semantic tier; request fingerprint; eligibility rules; single-flight; numpy vector index replaces FAISS (ADR-009); adaptive threshold removed (ADR-008); measured false-hit rates published. 364 tests pass (cache tests run against real Redis). |
| 6 Context compression | Done | Tool-call-safe splitting (a `tool` reply is never separated from its assistant call), per-conversation summary memoisation (bounded LRU), empty summaries rejected, runs on the miss path only (from Phase 5). 375 tests pass. |
| 7 Reliability | Not started | |
| 8 Database and telemetry | Not started | |
| 9 Security and access | Not started | |
| 10 Dashboard | Not started | |
| 11 Performance and benchmarking | Not started | |
| 12 Testing and quality | Not started | |
| 13 Deployment and operations | Not started | |
| 14 Final integration and release | Not started | |

## Known blockers and limitations

- **Docker daemon is not running** on the development machine, so image builds are unverified until it is started.
- **No provider API keys** are available. Everything touching providers is tested against a local fake upstream; live E2E tests stay excluded.
- **Push access works** (verified by the first push). There is no `gh` CLI on the machine, so pull requests and issues are not created from here; the branch is pushed and a PR can be opened from the link GitHub prints.
- Installed `redis-server` is 8.10.3 (Homebrew); `docker-compose.yml` pins `redis:7-alpine`. Integration tests should run on both once Docker is available.

## Notes carried forward

- **Phase 2:** the lock resolved newer versions than the baseline install (litellm 1.95.1 → 1.109.1, starlette 0.41.3 → 0.46.2, pydantic 2.10.5 → 2.14.3). The suite passes on them.
- **LiteLLM deprecation:** `Router.model_list` (used in `app/proxy.py`) is deprecated in 1.109; replace with `Router.registry().all()` when the provider layer is reworked in Phase 4.
- **One existing test was edited**, deliberately: `test_proxy.py` and `test_e2e.py` asserted `version == "1.0.0"`; they now compare to `app.__version__`, because the version is intentionally bumped to `2.0.0.dev0`. No other pre-existing test was changed.
- `app/errors.py` (OpenAI-shaped errors) moved from Phase 2 to Phase 3, where it is first used.

- **Phase 3, deliberate changes to six existing tests** (the contract changed on purpose, see ADR-007): four `422` → `400`; upstream `429` body `code` `"429"` → `"upstream_rate_limit"`; upstream `500` → `502`. The "rejected requests aren't recorded in telemetry" test still verifies the same property.
- **Phase 3 acceptance, re-run live:** the baseline's 10 MB request now returns `413` in under 1 ms (baseline: process frozen for more than 4 minutes); `/health` answered in 1–4 ms while a 1 MB whitespace-free prompt was in flight; a streaming request against an unreachable provider returned a real `502` JSON error instead of a truncated stream.
- **Phase 3 follow-ups not done yet:** provider error messages are truncated/redacted but still contain LiteLLM's text (fixed messages come in Phase 7); an embedding or FAISS exception becomes a 500 instead of degrading to a miss (Phase 5); streaming requests record zero tokens (Phase 8); telemetry writes still swallow exceptions (Phase 8); `docs/API.md` previously documented `X-Cache-Hit` but the code sends `X-Cache` (doc fixed).

- **Phase 4:** the classifier's accuracy on the labeled set is unchanged at 92.8% after word-boundary matching and stem keywords (`analy`, `compar`, `optimiz`, `vulnerab`); no gain or loss was measured. Three `test_router.py` fixtures now pass `routing_mode="always"` because they test classification plus tier-to-model mapping, which only applies in that mode. The labeled-set "hard cases" extension is not done yet (Phase 11).
- Not done in Phase 4, on purpose: a learned classifier (no quality labels exist; see research F7).

- **Phase 5 measured results** (full method and limits in [benchmarks/CACHE_CORRECTNESS.md](benchmarks/CACHE_CORRECTNESS.md)): on 71 hand-labeled pairs at the default threshold 0.92, the raw semantic cache served 9 of 41 should-not-match pairs (22%) with 70% recall; with the guards, 3 of 41 (7%, a wide plausible range of about 2–20%) with 63% recall. The remaining false hits are lowercase word swaps (e.g. miles/kilometers), reproduced live on the real stack. Hence the semantic tier ships **off**.
- **Phase 5 verified live** against a local fake provider through the real LiteLLM path: exact hit about 29 ms vs about 530 ms for a miss with 300 ms simulated provider latency (single runs, not a benchmark); 8 concurrent identical requests produced 1 provider call; different temperature, different model and `bypass` all miss.
- **A bug found only by running the real stack:** the setting was named `semantic_cache_enabled` (env `SEMANTIC_CACHE_ENABLED`) while docs said `SEMANTIC_CACHE`, so enabling it did nothing. Fixed, and `tests/test_rename_compat.py` now checks that every variable in `.env.example` is a real setting.
- **Phase 5 deliberate test removals:** `test_adaptive.py` (feature removed), `test_cache.py` and `test_cache_invalidation.py` (tested the FAISS-based cache; replaced by `test_response_cache.py`, `test_cache_pipeline.py`, `test_vector_index.py`, `test_fingerprint.py`, `test_guards.py`), and the LiteLLM built-in-cache tests in `test_failover.py` (that redundant layer was removed). `faiss-cpu` is no longer a dependency; the embedder runs without `KMP_DUPLICATE_LIB_OK`.
- **Breaking in 2.0 from Phase 5:** `/v1/threshold*` answer 410; env vars `ENABLE_CACHE`, `REDIS_HOST/PORT/PASSWORD`, `FAISS_INDEX_PATH`, `ADAPTIVE_THRESHOLD_*` are gone (unknown env vars are ignored, so old `.env` files still load); the old `data/faiss.index*` files are unused; redis keys are now `nj:v2:r:*` (old `cache:*` keys are ignored and expire or can be deleted).
- **Open after Phase 5:** compression's tool-call safety and memoization (Phase 6); sentence-transformers/torch is still a hard dependency even though the model only loads when the semantic tier is on (Phase 13 makes it an extra and measures the image size).

- **Phase 6 not measured:** the token saving from compression was not measured with a real summariser (needs a provider or a fake that returns realistic summaries), so no saving is claimed. The summary is still inserted with `role: user` and delimited by a marker; I have not checked how every provider treats two consecutive user turns, so that remains an assumption.
- **Phase 6 test isolation:** the summary memo is process-global state, so `tests/conftest.py` clears it before every test (an existing compressor test failed from a leaked memo before this was added).

Updated after each phase.
