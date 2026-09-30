"""Token bucket used to stay under the upstream providers' IP limits."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Refills at `rate` tokens/second, never holding more than `capacity`.

    Deezer starts answering with ExceedingLimit around 50 requests / 5 s per IP
    and iTunes tolerates roughly 20 requests / minute. The bucket keeps our own
    traffic below that even when a crowd types the same thing at once.
    """

    def __init__(self, rate: float, capacity: int) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    def try_acquire(self) -> bool:
        """Take a token if one is free, never waiting. For per-user throttling,
        where queueing the request would be worse than dropping it.

        No lock: there is no await between the refill and the decrement, so the
        whole thing is atomic as far as the event loop is concerned.
        """
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def seconds_until_free(self) -> float:
        self._refill()
        if self._tokens >= 1:
            return 0.0
        return (1 - self._tokens) / self._rate

    async def acquire(self, timeout: float) -> bool:
        """Take one token. Returns False if it would take longer than `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
                wait_for = (1 - self._tokens) / self._rate

            if time.monotonic() + wait_for > deadline:
                return False
            await asyncio.sleep(wait_for)
