"""Redis-backed cache, so several bot replicas share one cache."""

from __future__ import annotations

import logging
from typing import Optional

from redis.asyncio import Redis

from .base import CacheBackend, CacheEntry

log = logging.getLogger(__name__)


class RedisCache(CacheBackend):
    def __init__(self, url: str, namespace: str = "music") -> None:
        self._redis: Redis = Redis.from_url(url, encoding="utf-8", decode_responses=True)
        self._ns = namespace

    def _key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    async def get(self, key: str) -> Optional[CacheEntry]:
        try:
            raw = await self._redis.get(self._key(key))
        except Exception as exc:  # a dead cache must not break search
            log.warning("redis get failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return CacheEntry.loads(raw)
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("dropping malformed cache entry %s: %s", key, exc)
            return None

    async def set(self, key: str, entry: CacheEntry, ttl: int) -> None:
        try:
            await self._redis.set(self._key(key), entry.dumps(), ex=ttl)
        except Exception as exc:
            log.warning("redis set failed: %s", exc)

    async def close(self) -> None:
        await self._redis.aclose()
