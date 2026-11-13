"""In-process vector index for the semantic cache tier.

Brute-force cosine search over L2-normalised float32 vectors, scoped to a
*partition* (see app/fingerprint.py). This replaces the FAISS flat index: at
10k–100k vectors both take well under 3 ms per query on a laptop (measured,
docs/research/TECHNICAL_FINDINGS.md F3), and a numpy matrix gives partition
filtering, deletes and per-entry expiry directly. FAISS also forced the
``KMP_DUPLICATE_LIB_OK`` workaround on macOS.

Limits worth knowing: search cost is linear in the entries of the searched
partition; this is not meant for millions of vectors. State is per process and
persisted to a local file, so replicas do not share it.

Concurrency: all mutations and searches take one lock. Persistence copies the
arrays under the lock and writes outside it.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import structlog

logger = structlog.get_logger()

_INITIAL_CAPACITY = 1024


class VectorIndex:
    def __init__(self, dimension: int, max_entries: int = 50_000) -> None:
        self.dimension = dimension
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._reset_locked()
        self.dirty = False

    # ── internal state ───────────────────────────────────────────────────

    def _reset_locked(self) -> None:
        self._vecs = np.zeros((_INITIAL_CAPACITY, self.dimension), dtype=np.float32)
        self._expires = np.zeros(_INITIAL_CAPACITY, dtype=np.float64)
        self._alive = np.zeros(_INITIAL_CAPACITY, dtype=bool)
        self._keys: list[str] = []
        self._parts: list[str] = []
        self._n = 0                      # rows used (alive or dead)
        self._live = 0
        self._key_row: dict[str, int] = {}
        self._part_rows: dict[str, list[int]] = {}
        self._oldest = 0                 # scan pointer for FIFO eviction

    def _grow_locked(self) -> None:
        cap = self._vecs.shape[0] * 2
        self._vecs = np.concatenate([self._vecs, np.zeros_like(self._vecs)])[:cap]
        self._expires = np.concatenate([self._expires, np.zeros(cap - self._expires.shape[0])])
        self._alive = np.concatenate([self._alive, np.zeros(cap - self._alive.shape[0], dtype=bool)])

    def _kill_row_locked(self, row: int) -> None:
        if self._alive[row]:
            self._alive[row] = False
            self._live -= 1
        key = self._keys[row]
        if self._key_row.get(key) == row:
            del self._key_row[key]

    def _compact_locked(self) -> None:
        keep = np.flatnonzero(self._alive[: self._n])
        vecs, exp = self._vecs[keep].copy(), self._expires[keep].copy()
        keys = [self._keys[i] for i in keep]
        parts = [self._parts[i] for i in keep]
        self._reset_locked()
        for i, (k, p) in enumerate(zip(keys, parts, strict=True)):
            while i >= self._vecs.shape[0]:
                self._grow_locked()
            self._vecs[i], self._expires[i], self._alive[i] = vecs[i], exp[i], True
            self._keys.append(k)
            self._parts.append(p)
            self._key_row[k] = i
            self._part_rows.setdefault(p, []).append(i)
        self._n = self._live = len(keys)

    # ── public API ───────────────────────────────────────────────────────

    def __len__(self) -> int:
        return self._live

    def add(self, key: str, partition: str, vector: np.ndarray, expires_at: float) -> None:
        """Insert or replace the entry for ``key``. ``vector`` must be L2-normalised."""
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        if vec.shape[0] != self.dimension:
            raise ValueError(f"expected dimension {self.dimension}, got {vec.shape[0]}")
        with self._lock:
            old = self._key_row.get(key)
            if old is not None:
                self._kill_row_locked(old)
            if self._n >= self._vecs.shape[0]:
                if self._live < self._n * 0.75:
                    self._compact_locked()
                if self._n >= self._vecs.shape[0]:
                    self._grow_locked()
            row = self._n
            self._vecs[row], self._expires[row], self._alive[row] = vec, expires_at, True
            self._keys.append(key)
            self._parts.append(partition)
            self._key_row[key] = row
            self._part_rows.setdefault(partition, []).append(row)
            self._n += 1
            self._live += 1
            self.dirty = True
            self._evict_over_capacity_locked()

    def _evict_over_capacity_locked(self) -> None:
        while self._live > self.max_entries and self._oldest < self._n:
            if self._alive[self._oldest]:
                self._kill_row_locked(self._oldest)
            self._oldest += 1

    def search(
        self, partition: str, vector: np.ndarray, *, now: float | None = None, k: int = 5,
    ) -> list[tuple[str, float]]:
        """Top-k (key, cosine similarity) within ``partition``, ignoring dead and expired entries."""
        now = time.time() if now is None else now
        q = np.asarray(vector, dtype=np.float32).reshape(-1)
        with self._lock:
            rows = self._part_rows.get(partition)
            if not rows:
                return []
            idx = np.asarray(rows, dtype=np.int64)
            ok = self._alive[idx] & (self._expires[idx] > now)
            idx = idx[ok]
            if idx.size == 0:
                return []
            scores = self._vecs[idx] @ q
            order = np.argsort(-scores)[:k]
            return [(self._keys[int(idx[i])], float(scores[i])) for i in order]

    def remove(self, key: str) -> bool:
        with self._lock:
            row = self._key_row.get(key)
            if row is None:
                return False
            self._kill_row_locked(row)
            self.dirty = True
            return True

    def remove_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            expired = np.flatnonzero(self._alive[: self._n] & (self._expires[: self._n] <= now))
            for row in expired:
                self._kill_row_locked(int(row))
            if expired.size:
                self.dirty = True
                if self._live < self._n * 0.5:
                    self._compact_locked()
            return int(expired.size)

    def flush(self) -> int:
        with self._lock:
            count = self._live
            self._reset_locked()
            self.dirty = True
            return count

    # ── persistence ──────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Write a snapshot atomically (temp file + rename), so a crash never leaves a torn file."""
        with self._lock:
            keep = np.flatnonzero(self._alive[: self._n])
            vecs = self._vecs[keep].copy()
            expires = self._expires[keep].copy()
            keys = [self._keys[i] for i in keep]
            parts = [self._parts[i] for i in keep]
            self.dirty = False
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                np.savez(f, vecs=vecs, expires=expires, keys=np.array(keys, dtype=str),
                         parts=np.array(parts, dtype=str), dim=np.array(self.dimension))
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            self.dirty = True
            raise

    def load(self, path: str, *, now: float | None = None) -> int:
        """Load a snapshot. Returns entries loaded. A missing, corrupt or dimension-mismatched file is ignored."""
        now = time.time() if now is None else now
        if not os.path.exists(path):
            return 0
        try:
            data = np.load(path, allow_pickle=False)
            if int(data["dim"]) != self.dimension:
                logger.warning("vector_index.dimension_mismatch", file_dim=int(data["dim"]), expected=self.dimension)
                return 0
            vecs, expires = data["vecs"], data["expires"]
            keys, parts = [str(k) for k in data["keys"]], [str(p) for p in data["parts"]]
        except Exception as exc:  # noqa: BLE001 - a bad cache file must never stop startup
            logger.warning("vector_index.load_failed", path=path, reason=str(exc))
            return 0
        loaded = 0
        for i, key in enumerate(keys):
            if expires[i] > now:
                self.add(key, parts[i], vecs[i], float(expires[i]))
                loaded += 1
        self.dirty = False
        return loaded
