# ═══════════════════════════════════════════════════════════════════════
#  NightJar — Multi-stage Production Dockerfile
# ═══════════════════════════════════════════════════════════════════════

# ─── Stage 1: Builder ─────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a virtual env for clean copy
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence-transformers embedding model at build time
# so it's cached in the image and not downloaded on every container start.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"


# ─── Stage 2: Runtime ────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install only runtime system deps (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -r -s /bin/bash appuser

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy pre-downloaded model cache from builder
COPY --from=builder /root/.cache /home/appuser/.cache

# Copy application code
COPY app/ ./app/
COPY config/ ./config/
COPY docs/dashboard/ ./docs/dashboard/

# Create data directory for SQLite and FAISS persistence
RUN mkdir -p /app/data && chown -R appuser:appuser /app /home/appuser/.cache

# Switch to non-root user
USER appuser

# Secure by default in a container: no keys configured means every protected request is refused
# (and a startup error explains how to create one), instead of an open gateway holding provider keys.
ENV NIGHTJAR_AUTH=required

EXPOSE 8000

# Health check — verifies the app is responding
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
