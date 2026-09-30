"""Cache contract.

The storage layer only knows about a hard TTL (`stale_ttl`) — how long an entry
may exist at all. Freshness (the 5 minute window) is decided by the service from
`created_at`, which lets a stale-but-existing entry be served as a fallback when
every provider is down instead of showing the user an empty list.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from ..models import SearchPage


@dataclass
class CacheEntry:
    page: SearchPage
    created_at: float

    def age(self) -> float:
        return max(0.0, time.time() - self.created_at)

    def is_fresh(self, fresh_ttl: float) -> bool:
        return self.age() < fresh_ttl

    def dumps(self) -> str:
        return json.dumps(
            {"created_at": self.created_at, "page": self.page.to_dict()},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def loads(cls, raw: str) -> "CacheEntry":
        data: dict[str, Any] = json.loads(raw)
        return cls(page=SearchPage.from_dict(data["page"]), created_at=float(data["created_at"]))


class CacheBackend(ABC):
    @abstractmethod
    async def get(self, key: str) -> Optional[CacheEntry]:
        ...

    @abstractmethod
    async def set(self, key: str, entry: CacheEntry, ttl: int) -> None:
        ...

    async def close(self) -> None:
        return None
