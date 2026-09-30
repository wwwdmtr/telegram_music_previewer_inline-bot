"""iTunes Search API — fallback for when Deezer is down or throttling us.

    GET https://itunes.apple.com/search?term=...&media=music&entity=song&limit=20&offset=0

Also key-less, but the documented budget is about 20 requests per minute, so it
is deliberately kept as a second choice rather than a primary source.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..models import SearchPage, Track
from ..text import sanitize_for_upstream
from .base import ProviderError, ProviderRateLimited, SearchProvider

log = logging.getLogger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"


class ITunesProvider(SearchProvider):
    name = "itunes"

    def __init__(self, client: httpx.AsyncClient, rate: float, burst: int, timeout: float) -> None:
        super().__init__(rate=rate, burst=burst)
        self._client = client
        self._timeout = timeout

    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        cleaned = sanitize_for_upstream(query)
        if not cleaned:
            return SearchPage(provider=self.name)

        await self._reserve(self._timeout)
        params = {
            "term": cleaned,
            "media": "music",
            "entity": "song",
            "limit": limit,
            "offset": offset,
        }
        try:
            response = await self._client.get(SEARCH_URL, params=params, timeout=self._timeout)
            response.raise_for_status()
            # iTunes answers with content-type text/javascript, so json() needs a nudge.
            payload: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429):
                raise ProviderRateLimited(f"itunes: HTTP {exc.response.status_code}") from exc
            raise ProviderError(f"itunes: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"itunes: {type(exc).__name__}: {exc}") from exc

        results = payload.get("results") or []
        tracks: list[Track] = []
        for item in results:
            preview = (item.get("previewUrl") or "").strip()
            if not preview:
                continue
            artwork = item.get("artworkUrl100") or item.get("artworkUrl60") or ""
            tracks.append(
                Track(
                    id=str(item.get("trackId") or preview),
                    title=(item.get("trackName") or "Unknown").strip(),
                    artist=(item.get("artistName") or "Unknown artist").strip(),
                    duration=int((item.get("trackTimeMillis") or 0) // 1000),
                    preview_url=preview,
                    provider=self.name,
                    album=(item.get("collectionName") or "").strip(),
                    cover_url=artwork.replace("100x100", "300x300"),
                    source_url=item.get("trackViewUrl") or "",
                    explicit=item.get("trackExplicitness") == "explicit",
                )
            )
        return SearchPage(tracks=tracks, has_more=len(results) >= limit, provider=self.name)
