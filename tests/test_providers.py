"""Provider parsing and error mapping, with mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from music_bot.providers import DeezerProvider, ITunesProvider
from music_bot.providers.base import ProviderError, ProviderRateLimited

DEEZER_SEARCH = "https://api.deezer.com/search"
ITUNES_SEARCH = "https://itunes.apple.com/search"


def deezer_item(track_id: int, preview: str = "https://cdn/p.mp3", **over):
    item = {
        "id": track_id,
        "title": f"Track {track_id}",
        "title_short": f"Track {track_id}",
        "duration": 200,
        "preview": preview,
        "explicit_lyrics": False,
        "link": f"https://www.deezer.com/track/{track_id}",
        "artist": {"name": "Artist"},
        "album": {"title": "Album", "cover_medium": "https://cdn/c.jpg"},
    }
    item.update(over)
    return item


@pytest.fixture
def client():
    return httpx.AsyncClient()


@pytest.fixture
def deezer(client):
    return DeezerProvider(client, rate=1000, burst=1000, timeout=2.0)


@pytest.fixture
def itunes(client):
    return ITunesProvider(client, rate=1000, burst=1000, timeout=2.0)


@respx.mock
@pytest.mark.asyncio
async def test_deezer_parses_page_and_pagination(deezer):
    respx.get(DEEZER_SEARCH).mock(
        return_value=httpx.Response(
            200, json={"data": [deezer_item(1), deezer_item(2)], "next": "https://api.deezer.com/..."}
        )
    )
    page = await deezer.search("anything", 0, 20)

    assert [t.id for t in page.tracks] == ["1", "2"]
    assert page.tracks[0].artist == "Artist"
    assert page.tracks[0].album == "Album"
    assert page.has_more is True
    assert page.provider == "deezer"


@respx.mock
@pytest.mark.asyncio
async def test_deezer_drops_tracks_without_preview(deezer):
    """Region-locked tracks have an empty preview and cannot be played by Telegram."""
    respx.get(DEEZER_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    deezer_item(1, preview=""),
                    deezer_item(2, preview="   "),
                    deezer_item(3),
                ]
            },
        )
    )
    page = await deezer.search("anything", 0, 20)

    assert [t.id for t in page.tracks] == ["3"]
    assert page.has_more is False  # no "next" key


@respx.mock
@pytest.mark.asyncio
async def test_deezer_quota_error_is_rate_limited(deezer):
    """Deezer reports quota problems with HTTP 200 plus an error body."""
    respx.get(DEEZER_SEARCH).mock(
        return_value=httpx.Response(
            200, json={"error": {"type": "ExceedingLimit", "message": "Quota", "code": 4}}
        )
    )
    with pytest.raises(ProviderRateLimited):
        await deezer.search("anything", 0, 20)


@respx.mock
@pytest.mark.asyncio
async def test_deezer_other_error_is_plain_provider_error(deezer):
    respx.get(DEEZER_SEARCH).mock(
        return_value=httpx.Response(
            200, json={"error": {"type": "DataException", "message": "no data", "code": 800}}
        )
    )
    with pytest.raises(ProviderError) as exc:
        await deezer.search("anything", 0, 20)
    assert not isinstance(exc.value, ProviderRateLimited)


@respx.mock
@pytest.mark.asyncio
async def test_deezer_timeout_becomes_provider_error(deezer):
    respx.get(DEEZER_SEARCH).mock(side_effect=httpx.ConnectTimeout("too slow"))
    with pytest.raises(ProviderError):
        await deezer.search("anything", 0, 20)


@respx.mock
@pytest.mark.asyncio
async def test_deezer_http_429(deezer):
    respx.get(DEEZER_SEARCH).mock(return_value=httpx.Response(429))
    with pytest.raises(ProviderRateLimited):
        await deezer.search("anything", 0, 20)


@respx.mock
@pytest.mark.asyncio
async def test_deezer_sends_sanitized_query(deezer):
    route = respx.get(DEEZER_SEARCH).mock(return_value=httpx.Response(200, json={"data": []}))
    await deezer.search('artist:"x" (live)', 40, 20)

    request = route.calls[0].request
    assert request.url.params["q"] == "artist x live"
    assert request.url.params["index"] == "40"
    assert request.url.params["limit"] == "20"


@pytest.mark.asyncio
async def test_deezer_skips_request_for_blank_query(deezer):
    # No respx mock: any real request would fail, proving none was made.
    page = await deezer.search('"""', 0, 20)
    assert page.tracks == []


@respx.mock
@pytest.mark.asyncio
async def test_itunes_parses_and_upscales_artwork(itunes):
    respx.get(ITUNES_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "resultCount": 2,
                "results": [
                    {
                        "trackId": 11,
                        "trackName": "Song",
                        "artistName": "Artist",
                        "collectionName": "Album",
                        "previewUrl": "https://audio/p.m4a",
                        "artworkUrl100": "https://is1/100x100bb.jpg",
                        "trackTimeMillis": 215000,
                        "trackExplicitness": "explicit",
                        "trackViewUrl": "https://music.apple.com/x",
                    },
                    {"trackId": 12, "trackName": "No preview", "previewUrl": ""},
                ],
            },
        )
    )
    page = await itunes.search("anything", 0, 20)

    assert len(page.tracks) == 1
    track = page.tracks[0]
    assert track.duration == 215
    assert track.explicit is True
    assert track.cover_url == "https://is1/300x300bb.jpg"
    assert page.has_more is False  # fewer results than the requested limit


@respx.mock
@pytest.mark.asyncio
async def test_itunes_403_is_rate_limited(itunes):
    respx.get(ITUNES_SEARCH).mock(return_value=httpx.Response(403))
    with pytest.raises(ProviderRateLimited):
        await itunes.search("anything", 0, 20)


@pytest.mark.asyncio
async def test_local_rate_limiter_blocks_before_hitting_upstream(client):
    """Bucket empty + no refill within the timeout => fail fast, no HTTP call."""
    provider = DeezerProvider(client, rate=0.01, burst=1, timeout=0.05)
    with respx.mock:
        route = respx.get(DEEZER_SEARCH).mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        await provider.search("first", 0, 20)  # consumes the only token
        with pytest.raises(ProviderRateLimited):
            await provider.search("second", 0, 20)
        assert route.call_count == 1
