# Deployment

## Docker Compose (the supported way)

```bash
cp .env.example .env
#   set a provider key, e.g. OPENAI_API_KEY=...
#   set NIGHTJAR_API_KEYS=<a long random key you choose>   (or create scoped keys, below)
#   leave NIGHTJAR_AUTH unset: the image sets it to `required`
docker compose up --build -d
curl http://localhost:8000/health
```

| Piece | Notes |
|---|---|
| `app` | NightJar under uvicorn, **one worker**, non-root user (`appuser`), health check on `/health`, data in the `app-data` volume (`/app/data`: telemetry DB, key store, vector snapshot) |
| `redis` | `redis:7-alpine`, AOF on, `maxmemory 512mb` with `allkeys-lru`, **not published to the host**, reachable only on the compose network |
| `REDIS_URL` | The compose file sets `redis://redis:6379` for the app, overriding the value in `.env`. Inside a container `localhost` is the container itself; the `.env.example` default would silently leave the cache without Redis. |
| Auth | The image sets `NIGHTJAR_AUTH=required`. With no key configured every protected request returns 401 and the log says how to create one. **Do not add `NIGHTJAR_AUTH=` to `.env`**: `env_file` overrides the image's value (a test guards `.env.example` against this). |

Create scoped keys in the running container:

```bash
docker compose exec app python -m app.keys create --name backend --scopes chat --rpm 120 --models gpt-4o-mini
docker compose exec app python -m app.keys list
docker compose exec app python -m app.keys revoke backend      # effective on the server within 30 s
```

Redis holds cached responses **and the text of prompts** (semantic tier). If you must inspect it: `docker compose exec redis redis-cli`. If you expose it anyway, set a password in `REDIS_URL` (`redis://:password@redis:6379`).

## Verified, and what that means

Run on 2026-11-13, Docker Desktop 29.9.2, linux/arm64 (Apple M4), against a local fake provider (no real provider key):

| Checked | Result |
|---|---|
| `docker compose build` | Succeeds. Image **2.55 GB**: the Python packages are 1.69 GB of that (CPU-only PyTorch 2.15.0+cpu, CUDA unavailable, no `nvidia-*`/`triton` packages), plus the pre-downloaded embedding model (about 92 MB) |
| Container health | `healthy`; `/health` reports `redis: ok` |
| Runs as non-root | `appuser` |
| Zero keys, defaults from `.env.example` | chat 401, cache flush 401, health 200, startup error log with the fix |
| With a key | first request `X-Cache: MISS`, identical second request `X-Cache: HIT` (proves the container talks to its own Redis); the provider saw exactly **1** request |
| Redis exposure | port 6379 not reachable from the host |
| Dashboard | served at `/dashboard/` |
| Embedding model | loads inside the container from the image cache |

**Not verified:** an x86_64 build (the CUDA-avoidance steps matter most there, where the default torch wheel is CUDA; I could only observe arm64, where the default wheel is already CPU-only); a build on a clean machine without a warm cache; resource limits (`mem_limit`, CPU) under load; a long-running soak; behaviour with a real provider; TLS (terminate it in front of the app: nginx, Caddy, a cloud load balancer).

Build options: `docker build --build-arg PRELOAD_EMBEDDING_MODEL=false .` skips the model download (about 90 MB) if you never enable `SEMANTIC_CACHE`. The semantic tier is the only reason the image contains PyTorch and sentence-transformers at all; making them an optional install so a lean image is possible is **not done**.

## Resources

Measured (one sample each, [GATEWAY_OVERHEAD](../benchmarks/GATEWAY_OVERHEAD.md)): process RSS about 280–320 MB with the semantic tier off, about 600 MB on. Vector memory is `entries × 384 × 4 bytes` (about 77 MB at the default 50,000-entry cap). Redis memory is bounded by `maxmemory`.

## Operations

| | |
|---|---|
| Health | `GET /health`: liveness plus `redis`, `upstream` (in flight / limit / shed) and `telemetry_write_failures`. Stays `200` when Redis is down (the cache degrades to misses); check the `redis` field. |
| Logs | Structured (structlog) to stdout; no prompt content. Key events: `auth.*`, `upstream.error`, `cache.*`, `telemetry.write_failed`, `deprecated.*` |
| Backups | `telemetry.db` and `api_keys` (same SQLite file, `/app/data/telemetry.db`): copy the file while the app is stopped, or use `sqlite3 .backup`. The vector snapshot (`vectors.npz`) and Redis are caches: losing them costs hits, not data. |
| Upgrades | Back up the SQLite file, then start the new version; migrations are additive and automatic. See [MIGRATION_2.0](MIGRATION_2.0.md). |
| Retention | `TELEMETRY_RETENTION_DAYS=N` deletes older rows at startup. Off by default. |
| Scaling | **One instance.** Vector index, SQLite, single-flight, cooldowns and rate limits are per process, so replicas and multi-worker setups would not share them ([SCALABILITY](../architecture/SCALABILITY.md)). Do not run `uvicorn --workers N`. |

## Without Docker

```bash
uv sync --frozen
cp .env.example .env
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Bind to `127.0.0.1` unless something in front handles TLS and you have created keys. With no keys a local install is open by design; `NIGHTJAR_AUTH=required` closes it.

## Troubleshooting

[TROUBLESHOOTING](TROUBLESHOOTING.md).
