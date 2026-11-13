"""API keys: hashed storage, scopes, per-key limits, and a small CLI.

    uv run python -m app.keys create --name backend --scopes chat --rpm 60 --models gpt-4o-mini
    uv run python -m app.keys list
    uv run python -m app.keys revoke backend

Only a SHA-256 of the key is stored. The key itself is shown once, at creation. Keys are 256 random
bits, so a plain (unsalted, fast) hash is appropriate here: there is nothing to brute-force, unlike a
human-chosen password. Revocation takes effect within AUTH_CACHE_SECONDS because lookups are cached.

Scopes: ``chat`` (use /v1/chat/completions) and ``admin`` (telemetry, cache admin, /admin/*).
Keys from the NIGHTJAR_API_KEYS environment variable are legacy "env keys" with both scopes.
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field

SCOPES = frozenset({"chat", "admin"})
KEY_PREFIX = "nj-"
AUTH_CACHE_SECONDS = 30.0


@dataclass(frozen=True)
class Principal:
    """Who is calling. ``name`` is what telemetry attributes spend to."""

    name: str
    scopes: frozenset[str] = field(default_factory=lambda: SCOPES)
    allowed_models: tuple[str, ...] = ()
    rate_limit_rpm: int | None = None
    key_id: int | None = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes


ANONYMOUS = Principal(name="anonymous")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class KeyStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._cache: dict[str, tuple[float, Principal | None]] = {}
        self._has_keys: tuple[float, bool] = (0.0, False)
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        from pathlib import Path

        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_hint TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    allowed_models TEXT NOT NULL DEFAULT '',
                    rate_limit_rpm INTEGER,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                )""")

    # ── management ───────────────────────────────────────────────────────

    def create(self, name: str, scopes: set[str], allowed_models: list[str] | None = None,
               rate_limit_rpm: int | None = None) -> str:
        bad = scopes - SCOPES
        if bad or not scopes:
            raise ValueError(f"scopes must be a non-empty subset of {sorted(SCOPES)}; got {sorted(scopes)}")
        if not name.strip():
            raise ValueError("name is required")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with closing(self._conn()) as conn, conn:
            try:
                conn.execute(
                    "INSERT INTO api_keys (name, key_hash, key_hint, scopes, allowed_models, rate_limit_rpm,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, hash_key(key), key[:7] + "…" + key[-4:], ",".join(sorted(scopes)),
                     ",".join(allowed_models or []), rate_limit_rpm,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"a key named {name!r} already exists") from exc
        self._cache.clear()
        self._has_keys = (0.0, False)
        return key

    def revoke(self, name: str) -> bool:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), name),
            )
        self._cache.clear()
        self._has_keys = (0.0, False)
        return cur.rowcount > 0

    def list(self) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT name, key_hint, scopes, allowed_models, rate_limit_rpm, created_at, revoked_at"
                " FROM api_keys ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── lookup ───────────────────────────────────────────────────────────

    def has_active_keys(self) -> bool:
        ts, value = self._has_keys
        if time.monotonic() - ts < AUTH_CACHE_SECONDS and ts:
            return value
        with closing(self._conn()) as conn:
            value = conn.execute("SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1").fetchone() is not None
        self._has_keys = (time.monotonic(), value)
        return value

    def authenticate(self, token: str) -> Principal | None:
        digest = hash_key(token)
        hit = self._cache.get(digest)
        if hit and time.monotonic() - hit[0] < AUTH_CACHE_SECONDS:
            return hit[1]
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT id, name, scopes, allowed_models, rate_limit_rpm FROM api_keys"
                " WHERE key_hash = ? AND revoked_at IS NULL", (digest,),
            ).fetchone()
        principal = None
        if row:
            principal = Principal(
                name=row["name"], scopes=frozenset(row["scopes"].split(",")),
                allowed_models=tuple(m for m in row["allowed_models"].split(",") if m),
                rate_limit_rpm=row["rate_limit_rpm"], key_id=row["id"],
            )
        if len(self._cache) > 1024:
            self._cache.clear()
        self._cache[digest] = (time.monotonic(), principal)
        return principal


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings

    ap = argparse.ArgumentParser(prog="python -m app.keys", description="Manage NightJar API keys.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create a key (shown once)")
    c.add_argument("--name", required=True)
    c.add_argument("--scopes", default="chat", help="comma-separated: chat,admin (default chat)")
    c.add_argument("--models", default="", help="comma-separated allow-list; empty = any")
    c.add_argument("--rpm", type=int, default=None, help="requests per minute limit")
    sub.add_parser("list", help="list keys (hints only, never the key)")
    r = sub.add_parser("revoke", help="revoke a key by name")
    r.add_argument("name")
    args = ap.parse_args(argv)

    store = KeyStore(get_settings().sqlite_db_path)
    if args.cmd == "create":
        try:
            key = store.create(args.name, set(args.scopes.split(",")),
                               [m for m in args.models.split(",") if m], args.rpm)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"Created key {args.name!r}. Store it now; it cannot be shown again:\n\n  {key}\n")
    elif args.cmd == "list":
        for k in store.list():
            state = "revoked" if k["revoked_at"] else "active"
            print(f"{k['name']:20} {k['key_hint']:16} scopes={k['scopes']:11} rpm={k['rate_limit_rpm']} "
                  f"models={k['allowed_models'] or '*'} {state}")
    else:
        ok = store.revoke(args.name)
        print("revoked" if ok else "no active key with that name")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
