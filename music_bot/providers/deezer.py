"""Deezer public API — no key, no login.

    GET https://api.deezer.com/search?q=...&index=0&limit=20
    GET https://api.deezer.com/chart/0/tracks?limit=20

Errors come back as HTTP 200 with an `error` object in the body, so the status
code alone is not enough to tell success from failure.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..models import SearchPage, Track
from ..text import sanitize_for_upstream
from .base import ProviderError, ProviderRateLimited, SearchProvider

log = logging.getLogger(__name__)

BASE_URL = "https://api.deezer.com"
# Deezer error codes: 4 = quota exceeded, 700 = service busy.
_RATE_LIMIT_CODES = {4, 700}


class DeezerProvider(SearchProvider):
    name = "deezer"

    def __init__(self, client: httpx.AsyncClient, rate: float, burst: int, timeout: float) -> None:
        super().__init__(rate=rate, burst=burst)
        self._client = client
        self._timeout = timeout

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._reserve(self._timeout)
        try:
            response = await self._client.get(
                f"{BASE_URL}{path}", params=params, timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise ProviderRateLimited("deezer: HTTP 429") from exc
            raise ProviderError(f"deezer: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"deezer: {type(exc).__name__}: {exc}") from exc

        if not isinstance(payload, dict):
            raise ProviderError("deezer: unexpected payload shape")

        error = payload.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            kind = error.get("type", "Unknown") if isinstance(error, dict) else str(error)
            if code in _RATE_LIMIT_CODES or "Limit" in str(kind):
                raise ProviderRateLimited(f"deezer: {kind} (code={code})")
            raise ProviderError(f"deezer: {kind} (code={code})")

        return payload

    def _parse(self, payload: dict[str, Any]) -> SearchPage:
        tracks: list[Track] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            preview = (item.get("preview") or "").strip()
            # Region-locked tracks come back with an empty preview. Telegram
            # cannot play those, so they must never reach the result list.
            if not preview:
                continue
            artist = (item.get("artist") or {}).get("name") or "Unknown artist"
            album = item.get("album") or {}
            tracks.append(
                Track(
                    id=str(item.get("id")),
                    title=(item.get("title_short") or item.get("title") or "Unknown").strip(),
                    artist=artist.strip(),
                    duration=int(item.get("duration") or 0),
                    preview_url=preview,
                    provider=self.name,
                    album=(album.get("title") or "").strip(),
                    cover_url=album.get("cover_medium") or album.get("cover") or "",
                    source_url=item.get("link") or "",
                    explicit=bool(item.get("explicit_lyrics")),
                )
            )
        # `next` is present only while more pages exist.
        return SearchPage(tracks=tracks, has_more=bool(payload.get("next")), provider=self.name)

    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        cleaned = sanitize_for_upstream(query)
        if not cleaned:
            return SearchPage(provider=self.name)
        payload = await self._request(
            "/search", {"q": cleaned, "index": offset, "limit": limit, "order": "RANKING"}
        )
        return self._parse(payload)

    async def chart(self, limit: int) -> SearchPage:
        payload = await self._request("/chart/0/tracks", {"limit": limit})
        page = self._parse(payload)
        page.has_more = False  # charts are a fixed top-N, not a paginated list
        return page
