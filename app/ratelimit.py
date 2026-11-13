"""Per-caller request rate limiting (token bucket, in process).

Capacity equals the per-minute limit, refilled continuously, so a caller may burst up to a minute's
worth and is then held to the average. Per process: with N workers a caller can get N times the
limit; a shared limiter would need Redis and is not built.
"""

from __future__ import annotations

import time

from app.errors import NightJarError


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}  # name -> (tokens, last_refill)
        self.rejected = 0

    def check(self, name: str, rpm: int | None, *, now: float | None = None) -> None:
        if not rpm:
            return
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(name, (float(rpm), now))
        tokens = min(float(rpm), tokens + (now - last) * rpm / 60.0)
        if tokens < 1.0:
            self.rejected += 1
            wait = max(1, int((1.0 - tokens) * 60.0 / rpm) + 1)
            self._buckets[name] = (tokens, now)
            raise NightJarError(
                429, f"Rate limit of {rpm} requests/minute exceeded for this API key.",
                type="rate_limit_error", code="rate_limit_exceeded", headers={"Retry-After": str(wait)},
            )
        self._buckets[name] = (tokens - 1.0, now)
        if len(self._buckets) > 10_000:
            self._buckets.clear()
