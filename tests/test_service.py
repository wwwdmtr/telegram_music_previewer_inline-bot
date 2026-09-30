"""Cache/fallback behaviour of SearchService, with fake providers."""

from __future__ import annotations

import asyncio

import pytest

from music_bot.cache import MemoryCache
from music_bot.models import SearchPage, Track
from music_bot.providers.base import ProviderError, SearchProvider
from music_bot.service import SearchService, SearchUnavailable
from music_bot.text import _dedup_key, dedupe, normalize_query, sanitize_for_upstream


def track(title: str, artist: str = "A", provider: str = "fake") -> Track:
    return Track(
        id=f"{artist}-{title}",
        title=title,
        artist=artist,
        duration=200,
        preview_url="https://example.invalid/p.mp3",
        provider=provider,
    )


class FakeProvider(SearchProvider):
    def __init__(self, name: str = "fake", tracks=None, fail: bool = False, delay: float = 0.0):
        super().__init__(rate=1000, burst=1000)
        self.name = name
        self.calls = 0
        self._tracks = tracks if tracks is not None else [track("Song")]
        self._fail = fail
        self._delay = delay

    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise ProviderError(f"{self.name}: boom")
        return SearchPage(tracks=list(self._tracks), has_more=False, provider=self.name)


def make_service(providers, fresh_ttl=300, stale_ttl=1800) -> SearchService:
    return SearchService(
        providers=providers,
        cache=MemoryCache(max_entries=100, ttl=stale_ttl),
        fresh_ttl=fresh_ttl,
        stale_ttl=stale_ttl,
        page_size=20,
    )


@pytest.mark.asyncio
async def test_second_identical_query_is_served_from_cache():
    provider = FakeProvider()
    service = make_service([provider])

    first = await service.search("Creep")
    second = await service.search("  CREEP  ")  # normalizes to the same key

    assert provider.calls == 1
    assert first.source == "fake"
    assert second.source == "cache"
    assert service.stats.hits == 1


@pytest.mark.asyncio
async def test_different_offset_is_a_different_cache_key():
    provider = FakeProvider()
    service = make_service([provider])

    await service.search("Creep", offset=0)
    await service.search("Creep", offset=20)

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_empty_result_is_cached_too():
    provider = FakeProvider(tracks=[])
    service = make_service([provider])

    await service.search("asdkjhasd")
    result = await service.search("asdkjhasd")

    assert provider.calls == 1
    assert result.page.tracks == []
    assert result.source == "cache"


@pytest.mark.asyncio
async def test_falls_back_to_next_provider():
    broken = FakeProvider(name="broken", fail=True)
    working = FakeProvider(name="working")
    service = make_service([broken, working])

    result = await service.search("Creep")

    assert result.source == "working"
    assert broken.calls == 1 and working.calls == 1


@pytest.mark.asyncio
async def test_stale_cache_is_served_when_everything_fails():
    provider = FakeProvider()
    # fresh_ttl=0 makes every stored entry immediately stale
    service = make_service([provider], fresh_ttl=0)

    await service.search("Creep")  # populates the cache
    provider._fail = True

    result = await service.search("Creep")

    assert result.source == "stale-cache"
    assert [t.title for t in result.page.tracks] == ["Song"]
    assert service.stats.stale_hits == 1


@pytest.mark.asyncio
async def test_raises_when_no_cache_and_all_providers_fail():
    service = make_service([FakeProvider(fail=True)])
    with pytest.raises(SearchUnavailable):
        await service.search("Creep")


@pytest.mark.asyncio
async def test_concurrent_identical_queries_hit_upstream_once():
    provider = FakeProvider(delay=0.05)
    service = make_service([provider])

    results = await asyncio.gather(*(service.search("Creep") for _ in range(10)))

    assert provider.calls == 1
    assert service.stats.coalesced == 9
    assert all(r.page.tracks for r in results)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_break_the_others():
    provider = FakeProvider(delay=0.1)
    service = make_service([provider])

    first = asyncio.create_task(service.search("Creep"))
    await asyncio.sleep(0)  # let the leader register itself
    second = asyncio.create_task(service.search("Creep"))
    await asyncio.sleep(0.01)
    first.cancel()  # user kept typing, Telegram dropped this query

    result = await second
    assert result.page.tracks
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_duplicates_are_removed():
    provider = FakeProvider(
        tracks=[
            track("Creep"),
            track("Creep - 2009 Remaster"),
            track("Creep (feat. Somebody)"),
            track("Karma Police"),
        ]
    )
    service = make_service([provider])

    result = await service.search("radiohead")

    assert [t.title for t in result.page.tracks] == ["Creep", "Karma Police"]


def test_normalize_and_sanitize():
    assert normalize_query("  Daft   PUNK  ") == "daft punk"
    assert len(normalize_query("x" * 500)) == 100
    assert sanitize_for_upstream('artist:"Daft Punk" (live)') == "artist Daft Punk live"


def test_dedupe_keeps_order_and_first_occurrence():
    tracks = [track("A"), track("B"), track("a"), track("C")]
    assert [t.title for t in dedupe(tracks)] == ["A", "B", "C"]


# --- regressions: de-duplication must not eat distinct recordings ----------


@pytest.mark.parametrize(
    "titles",
    [
        ["Creep", "Creep - 2009 Remaster"],
        ["Creep", "Creep (feat. Somebody)"],
        ["Dracula", "Dracula (with JENNIE)"],
        ["One More Time", "One More Time (Radio Edit)"],
        ["Smells Like Teen Spirit", "Smells Like Teen Spirit [Remastered]"],
        ["Lampochki", "Lampochki (Explicit)"],
        ["Song", "Song feat. Somebody"],
    ],
)
def test_variants_of_one_recording_collapse(titles):
    assert len(dedupe([track(t) for t in titles])) == 1


@pytest.mark.parametrize(
    "titles",
    [
        # "with" is part of the name here, not a featuring marker.
        ["Down With the Sickness", "Down"],
        ["Stay With Me", "Stay"],
        ["With Or Without You", "One"],
        # Titles that consist only of a marker word keep their own identity.
        ["Clean", "Explicit"],
        ["Radio", "Radio Edit Song"],
        ["Extended Play", "Play"],
    ],
)
def test_distinct_tracks_survive(titles):
    assert len(dedupe([track(t) for t in titles])) == len(titles)


def test_marker_inside_a_word_is_not_stripped():
    """"ft" of "Swift" used to be treated as "feat." and truncated the key."""
    swift = track("Blank Space", artist="Taylor Swift")
    clean_bandit = track("Symphony", artist="Clean Bandit")
    assert _dedup_key(swift) == ("taylor swift", "blank space")
    assert _dedup_key(clean_bandit) == ("clean bandit", "symphony")


def test_two_artists_with_marker_names_do_not_collide():
    tracks = [track("Symphony", artist="Clean Bandit"), track("Symphony", artist="Explicit Band")]
    assert len(dedupe(tracks)) == 2
