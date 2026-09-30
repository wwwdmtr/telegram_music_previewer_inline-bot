"""Search orchestration: cache, request coalescing, provider fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from .cache import CacheBackend, CacheEntry
from .models import SearchPage
from .providers import ProviderError, ProviderUnsupported, SearchProvider
from .text import dedupe, normalize_query

log = logging.getLogger(__name__)


ProviderCall = Callable[[SearchProvider], Awaitable[SearchPage]]


class SearchUnavailable(Exception):
    """Every provider failed and there was nothing usable in the cache."""


@dataclass
class Stats:
    hits: int = 0
    stale_hits: int = 0
    misses: int = 0
    coalesced: int = 0
    upstream_errors: int = 0
    timeouts: int = 0
    per_provider: dict[str, int] = field(default_factory=dict)

    def as_text(self) -> str:
        total = self.hits + self.stale_hits + self.misses
        rate = (self.hits / total * 100) if total else 0.0
        providers = ", ".join(f"{k}={v}" for k, v in sorted(self.per_provider.items())) or "—"
        return (
            f"запросов: {total}\n"
            f"кэш: {self.hits} свежих / {self.stale_hits} протухших ({rate:.0f}% hit-rate)\n"
            f"склеено на лету: {self.coalesced}\n"
            f"ошибок апстрима: {self.upstream_errors}\n"
            f"превышений бюджета: {self.timeouts}\n"
            f"отдано провайдерами: {providers}"
        )


@dataclass
class SearchResult:
    page: SearchPage
    source: str  # "cache" | "stale-cache" | provider name


class SearchService:
    def __init__(
        self,
        providers: Sequence[SearchProvider],
        cache: CacheBackend,
        *,
        fresh_ttl: int = 300,
        stale_ttl: int = 1800,
        page_size: int = 20,
        total_timeout: float = 4.0,
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = list(providers)
        self._cache = cache
        self._fresh_ttl = fresh_ttl
        self._stale_ttl = stale_ttl
        self._page_size = page_size
        self._total_timeout = total_timeout
        # Request coalescing ("single flight"): when N users type the same thing
        # within the same few milliseconds, exactly one upstream call is made and
        # everybody awaits its result.
        self._inflight: dict[str, asyncio.Task[SearchPage]] = {}
        self.stats = Stats()

    @property
    def page_size(self) -> int:
        return self._page_size

    async def search(self, query: str, offset: int = 0) -> SearchResult:
        normalized = normalize_query(query)
        key = f"s:{normalized}:{offset}:{self._page_size}"
        return await self._get(key, lambda p: p.search(normalized, offset, self._page_size))

    async def chart(self) -> SearchResult:
        key = f"c:{self._page_size}"
        return await self._get(key, lambda p: p.chart(self._page_size))

    async def _get(self, key: str, call: ProviderCall) -> SearchResult:
        cached = await self._cache.get(key)
        if cached and cached.is_fresh(self._fresh_ttl):
            self.stats.hits += 1
            return SearchResult(page=cached.page, source="cache")

        self.stats.misses += 1
        try:
            page = await self._coalesced_fetch(key, call)
        except SearchUnavailable:
            # Serving a few-minutes-old answer beats serving nothing.
            if cached:
                self.stats.stale_hits += 1
                log.warning("serving stale cache for %s (age %.0fs)", key, cached.age())
                return SearchResult(page=cached.page, source="stale-cache")
            raise

        return SearchResult(page=page, source=page.provider)

    async def _coalesced_fetch(self, key: str, call: ProviderCall) -> SearchPage:
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_and_store(key, call))
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key, None))
        else:
            self.stats.coalesced += 1

        # shield: a cancelled inline query (user kept typing) must not cancel the
        # shared fetch other waiters depend on.
        return await asyncio.shield(task)

    async def _fetch_and_store(self, key: str, call: ProviderCall) -> SearchPage:
        # One ceiling for the whole chain. Per-provider timeouts alone stack up:
        # rate-limiter wait plus HTTP, once per provider. An inline query that
        # takes many seconds reads as a hung bot, so cap it and fall back.
        try:
            page = await asyncio.wait_for(self._fetch(call), self._total_timeout)
        except asyncio.TimeoutError as exc:
            self.stats.timeouts += 1
            raise SearchUnavailable(
                f"search exceeded {self._total_timeout}s budget"
            ) from exc
        page.tracks = dedupe(page.tracks)
        # Negative results are cached too, otherwise a popular typo hammers the
        # upstream on every keystroke.
        await self._cache.set(key, CacheEntry(page=page, created_at=time.time()), self._stale_ttl)
        self.stats.per_provider[page.provider] = (
            self.stats.per_provider.get(page.provider, 0) + 1
        )
        return page

    async def _fetch(self, call: ProviderCall) -> SearchPage:
        errors: list[str] = []
        for provider in self._providers:
            try:
                return await call(provider)
            except ProviderUnsupported as exc:
                errors.append(str(exc))
            except ProviderError as exc:
                self.stats.upstream_errors += 1
                log.warning("provider %s failed: %s", provider.name, exc)
                errors.append(str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # defensive: a parser bug must not kill the bot
                self.stats.upstream_errors += 1
                log.exception("provider %s raised unexpectedly", provider.name)
                errors.append(f"{provider.name}: {exc}")
        raise SearchUnavailable("; ".join(errors) or "no providers answered")
