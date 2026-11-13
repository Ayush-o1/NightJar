"""Bound the number of provider calls in flight.

Without a bound, a slow provider turns every incoming request into a waiting coroutine holding
memory and a connection, and the gateway fails late and all at once. With a bound, excess load is
refused immediately with 503 + Retry-After, which well-behaved clients back off from.

Only requests that actually call a provider hold a slot: cache hits and single-flight followers
do not. It is a counter, not a queue, on purpose: a queue just moves the unbounded wait.
Single event loop, so no locking is needed. Per process: N workers allow N x limit.
"""

from __future__ import annotations

from contextlib import contextmanager

from app.errors import overloaded


class ConcurrencyLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0
        self.rejected = 0

    def acquire(self) -> None:
        if self.in_flight >= self.limit:
            self.rejected += 1
            raise overloaded(self.limit)
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    @contextmanager
    def slot(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def stats(self) -> dict[str, int]:
        return {"limit": self.limit, "in_flight": self.in_flight, "rejected": self.rejected}
