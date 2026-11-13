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

# PyTorch from the CPU-only wheel index. The default Linux x86_64 wheel drags in CUDA libraries (gigabytes)
# that a CPU-only gateway never uses. The exported lock also lists torch's CUDA dependencies (nvidia-*,
# triton) as separate pins, so they are filtered out here or `pip install -r` would fetch them anyway.
# The torch version is taken from the lock so the two cannot drift.
COPY requirements.txt .
RUN TORCH_VERSION=$(grep -E '^torch==' requirements.txt | head -1 | cut -d= -f3 | cut -d' ' -f1) \
    && pip install --no-cache-dir "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cpu \
    && grep -viE '^(nvidia-|triton|cuda-|torch==)' requirements.txt > requirements.cpu.txt \
    && pip install --no-cache-dir -r requirements.cpu.txt

# Pre-download the embedding model so the semantic tier starts without network access. Only needed when
# SEMANTIC_CACHE=true; build with --build-arg PRELOAD_EMBEDDING_MODEL=false to skip it (about 90 MB).
ARG PRELOAD_EMBEDDING_MODEL=true
RUN if [ "$PRELOAD_EMBEDDING_MODEL" = "true" ]; then \
      python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"; \
    else mkdir -p /root/.cache; fi


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
COPY --from=builder --chown=appuser:appuser /root/.cache /home/appuser/.cache

# Copy application code
COPY app/ ./app/
COPY config/ ./config/

# Create data directory for SQLite and FAISS persistence
RUN mkdir -p /app/data && chown -R appuser:appuser /app

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
