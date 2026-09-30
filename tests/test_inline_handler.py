"""Inline handler branches: short query, empty query, not found, upstream down."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from music_bot.cache import MemoryCache
from music_bot.config import Settings
from music_bot.handlers.inline import handle_inline
from music_bot.models import SearchPage, Track
from music_bot.providers.base import ProviderError, ProviderUnsupported, SearchProvider
from music_bot.service import SearchService

# A syntactically valid stand-in: Settings now rejects malformed tokens.
TOKEN = "123456789:AAEdummytokenfortestsonly0123456789"


@dataclass
class FakeQuery:
    """Stands in for aiogram's InlineQuery: the handler only reads these fields."""

    query: str = ""
    offset: str = ""
    answered: dict[str, Any] = field(default_factory=dict)

    async def answer(self, **kwargs):
        self.answered = kwargs


class StubProvider(SearchProvider):
    name = "stub"

    def __init__(self, tracks=None, fail=False, chart_tracks=None):
        super().__init__(rate=1000, burst=1000)
        self._tracks = tracks or []
        self._chart = chart_tracks or []
        self._fail = fail

    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        if self._fail:
            raise ProviderError("stub down")
        return SearchPage(tracks=list(self._tracks), has_more=len(self._tracks) >= limit,
                          provider=self.name)

    async def chart(self, limit: int) -> SearchPage:
        if not self._chart:
            raise ProviderUnsupported("no chart")
        return SearchPage(tracks=list(self._chart), provider=self.name)


def track(n: int) -> Track:
    return Track(
        id=str(n),
        title=f"Song {n}",
        artist=f"Artist {n}",
        duration=200,
        preview_url=f"https://cdn/{n}.mp3",
        provider="stub",
    )


def settings(**over) -> Settings:
    base = dict(bot_token=TOKEN, page_size=2, min_query_length=2, inline_cache_time=300,
                error_cache_time=5)
    base.update(over)
    return Settings(**base)


def service(provider) -> SearchService:
    return SearchService([provider], MemoryCache(100, 1800), fresh_ttl=300, page_size=2)


@pytest.mark.asyncio
async def test_short_query_never_reaches_the_provider():
    provider = StubProvider(tracks=[track(1)])
    query = FakeQuery(query="a")

    await handle_inline(query, service(provider), settings())

    assert query.answered["results"] == []
    assert "минимум 2" in query.answered["button"].text
    assert query.answered["cache_time"] == 300


@pytest.mark.asyncio
async def test_empty_query_shows_the_chart():
    provider = StubProvider(chart_tracks=[track(1), track(2)])
    query = FakeQuery(query="")

    await handle_inline(query, service(provider), settings())

    assert len(query.answered["results"]) == 2
    assert query.answered["results"][0].type == "audio"


@pytest.mark.asyncio
async def test_nothing_found_returns_a_visible_explanation():
    query = FakeQuery(query="asdkjhqwe")

    await handle_inline(query, service(StubProvider(tracks=[])), settings())

    results = query.answered["results"]
    assert len(results) == 1
    assert results[0].type == "article"
    assert results[0].title == "Ничего не найдено"
    assert "asdkjhqwe" in results[0].description
    # A miss may be cached normally — it is a valid answer, not a failure.
    assert query.answered["cache_time"] == 300


@pytest.mark.asyncio
async def test_end_of_pagination_is_silent():
    """An empty second page means the list ended, not that nothing was found."""
    query = FakeQuery(query="whatever", offset="20")

    await handle_inline(query, service(StubProvider(tracks=[])), settings())

    assert query.answered["results"] == []
    assert "button" not in query.answered


@pytest.mark.asyncio
async def test_upstream_failure_is_reported_and_barely_cached():
    query = FakeQuery(query="whatever")

    await handle_inline(query, service(StubProvider(fail=True)), settings())

    results = query.answered["results"]
    assert results[0].title == "Сервис поиска недоступен"
    # Crucial: a failure must not be frozen in Telegram's cache for 5 minutes.
    assert query.answered["cache_time"] == 5


@pytest.mark.asyncio
async def test_next_offset_advances_by_page_size():
    provider = StubProvider(tracks=[track(1), track(2)])  # == page_size -> has_more
    query = FakeQuery(query="whatever", offset="2")

    await handle_inline(query, service(provider), settings())

    assert query.answered["next_offset"] == "4"
    assert query.answered["is_personal"] is False


@pytest.mark.asyncio
async def test_last_page_has_no_next_offset():
    provider = StubProvider(tracks=[track(1)])  # < page_size -> no more
    query = FakeQuery(query="whatever")

    await handle_inline(query, service(provider), settings())

    assert query.answered["next_offset"] == ""


@pytest.mark.asyncio
async def test_garbage_offset_is_treated_as_first_page():
    provider = StubProvider(tracks=[track(1)])
    query = FakeQuery(query="whatever", offset="not-a-number")

    await handle_inline(query, service(provider), settings())

    assert query.answered["next_offset"] == ""
    assert len(query.answered["results"]) == 1


# --- regressions ------------------------------------------------------------


@pytest.mark.parametrize("bad", ["<b", "<i>x</i>", "a & b", "5 < 6", "</code>"])
@pytest.mark.asyncio
async def test_html_in_query_is_escaped_in_the_not_found_message(bad):
    """message_text is parsed as HTML: an unescaped "<" makes the whole
    answerInlineQuery call fail, so the user would see nothing at all."""
    query = FakeQuery(query=bad)

    await handle_inline(query, service(StubProvider(tracks=[])), settings())

    content = query.answered["results"][0].input_message_content
    assert content.parse_mode == "HTML"
    assert "<" not in content.message_text.replace("&lt;", "")
    assert "&" not in content.message_text.replace("&lt;", "").replace("&gt;", "").replace(
        "&amp;", ""
    )


@pytest.mark.asyncio
async def test_deep_scroll_ends_the_list_instead_of_looping():
    """Clamping the offset used to re-serve the same page forever: the
    next_offset we returned was clamped back to the same value on the way in."""
    provider = StubProvider(tracks=[track(1), track(2)])  # always says has_more
    query = FakeQuery(query="whatever", offset="500")

    await handle_inline(query, service(provider), settings())

    assert query.answered["results"] == []
    assert query.answered.get("next_offset", "") == ""


@pytest.mark.asyncio
async def test_next_offset_stops_before_the_cap():
    provider = StubProvider(tracks=[track(1), track(2)])
    query = FakeQuery(query="whatever", offset="498")

    await handle_inline(query, service(provider), settings())

    assert query.answered["results"]  # this page is still served
    assert query.answered["next_offset"] == ""  # but there is no page after it
