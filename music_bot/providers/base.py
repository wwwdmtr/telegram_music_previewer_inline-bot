"""Provider contract and error taxonomy."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import SearchPage
from ..ratelimit import TokenBucket


class ProviderError(Exception):
    """Upstream could not answer this request."""


class ProviderRateLimited(ProviderError):
    """Upstream refused us for sending too much — back off, try the next one."""


class ProviderUnsupported(ProviderError):
    """This provider does not implement the requested operation (e.g. charts)."""


class SearchProvider(ABC):
    name: str = "base"

    def __init__(self, rate: float, burst: int) -> None:
        self._bucket = TokenBucket(rate=rate, capacity=burst)

    async def _reserve(self, timeout: float) -> None:
        if not await self._bucket.acquire(timeout):
            raise ProviderRateLimited(f"{self.name}: local rate limit, no slot within {timeout}s")

    @abstractmethod
    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        ...

    async def chart(self, limit: int) -> SearchPage:
        raise ProviderUnsupported(f"{self.name} has no chart endpoint")
