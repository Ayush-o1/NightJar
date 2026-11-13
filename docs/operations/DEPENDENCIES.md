# Dependencies

`pyproject.toml` declares ranges. `uv.lock` pins every package, including transitive ones, for all platforms. `requirements.txt` is generated from the lock and exists only because the Dockerfile installs with pip.

| Task | Command |
|---|---|
| Create the environment (runtime + dev tools) | `uv sync --frozen` |
| Add a runtime dependency | `uv add <pkg>`, then regenerate `requirements.txt` |
| Add a dev-only dependency | `uv add --dev <pkg>` |
| Upgrade everything within the declared ranges | `uv lock --upgrade`, run the suite, commit the new lock |
| Regenerate `requirements.txt` | `uv export --no-dev --no-hashes --no-emit-project --frozen > requirements.txt` (then restore the 3-line header; CI only compares the non-comment lines) |
| OpenTelemetry export (optional) | `uv sync --frozen --extra otel` |

CI fails if `requirements.txt` and `uv.lock` disagree.

## What changed at the rename

- **Removed:** `sqlmodel`, `sqlalchemy` (never imported), `aiofiles` (never imported), and `httpx`/`pytest`/`pytest-asyncio`/`ruff` from the runtime set (now dev group).
- **Moved to an extra:** the OpenTelemetry packages. They were installed unconditionally though the feature is opt-in.
- **Pinned:** `litellm` was `>=1.30.0`, so installs floated. It is now `>=1.95,<2` in `pyproject.toml` and exact in the lock (1.109.1 at the time of locking).

## Known issue

On Linux, `pip install sentence-transformers` can pull the CUDA build of PyTorch, which adds gigabytes to the image. Pointing pip at the CPU wheel index is the fix, scheduled for Phase 13 with an actual image build to measure the difference.
