"""In-process cache. Fine for a single bot instance."""

from __future__ import annotations

import asyncio
from typing import Optional

from cachetools import TTLCache

from .base import CacheBackend, CacheEntry


class MemoryCache(CacheBackend):
    def __init__(self, max_entries: int, ttl: int) -> None:
        self._store: TTLCache[str, CacheEntry] = TTLCache(maxsize=max_entries, ttl=ttl)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[CacheEntry]:
        async with self._lock:
            return self._store.get(key)

    async def set(self, key: str, entry: CacheEntry, ttl: int) -> None:
        # TTLCache carries a single TTL for the whole cache; it is configured
        # with the stale TTL, which is the longest an entry may live.
        async with self._lock:
            self._store[key] = entry

    @property
    def size(self) -> int:
        return len(self._store)
