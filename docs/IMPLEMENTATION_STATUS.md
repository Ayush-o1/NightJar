# Implementation status

Branch: `feature/nightjar-modernization` (from `main` @ `1a4819c`). Not merged to `main`.

| Phase | Status | Notes |
|---|---|---|
| 0 Discovery and baseline | Done | [BASELINE.md](BASELINE.md), [TECHNICAL_AUDIT.md](TECHNICAL_AUDIT.md) |
| 1 Product definition and research | Done | `docs/research/`, [ROADMAP.md](ROADMAP.md) |
| 2 Rename and foundations | Done | Rename with compatibility aliases; `pyproject.toml` + `uv.lock` + generated `requirements.txt`; single version source; unused deps and `OPENAI_BASE_URL` removed. 187 tests pass (175 original + 12 new). |
| 3 Core API and request lifecycle | Not started | |
| 4 Routing and provider layer | Not started | |
| 5 Cache correctness | Not started | |
| 6 Context compression | Not started | |
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

Updated after each phase.
