"""Per-user throttling and the overall search deadline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest
from aiogram.types import InlineQuery, Message

from music_bot.cache import MemoryCache
from music_bot.models import SearchPage
from music_bot.providers.base import SearchProvider
from music_bot.ratelimit import TokenBucket
from music_bot.service import SearchService, SearchUnavailable
from music_bot.throttle import ThrottleMiddleware, ThrottleStats


@dataclass
class FakeUser:
    id: int = 1


@dataclass
class FakeEvent:
    """Duck-typed stand-in; the middleware only needs isinstance checks to fail
    gracefully, so a plain object exercises the "no notification" path."""

    replies: List[str] = field(default_factory=list)

    async def answer(self, text, **kwargs):
        self.replies.append(text)


class FakeMessage(Message):
    """A real Message so the middleware's isinstance check passes."""

    def __init__(self):
        super().__init__(message_id=1, date=0, chat={"id": 1, "type": "private"})
        object.__setattr__(self, "_replies", [])

    async def answer(self, text, **kwargs):
        self._replies.append(text)
        return None


def middleware(**over) -> ThrottleMiddleware:
    base = dict(
        rate=1.0,
        burst=2,
        max_users=100,
        stats=ThrottleStats(),
        kind="message",
        notify=True,
        exempt=frozenset(),
    )
    base.update(over)
    return ThrottleMiddleware(**base)


async def _handler(event, data):
    return "handled"


def _data(user_id: int = 1) -> dict:
    return {"event_from_user": FakeUser(id=user_id)}


# --- the bucket itself ------------------------------------------------------


def test_try_acquire_never_blocks_and_refills():
    bucket = TokenBucket(rate=100.0, capacity=2)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False  # burst spent
    assert bucket.seconds_until_free() > 0


# --- throttling -------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_passes_then_throttles():
    mw = middleware(burst=2)
    event = FakeEvent()

    assert await mw(_handler, event, _data()) == "handled"
    assert await mw(_handler, event, _data()) == "handled"
    assert await mw(_handler, event, _data()) is None  # third is dropped


@pytest.mark.asyncio
async def test_users_are_throttled_independently():
    mw = middleware(burst=1)

    assert await mw(_handler, FakeEvent(), _data(user_id=1)) == "handled"
    assert await mw(_handler, FakeEvent(), _data(user_id=1)) is None
    # A different user still has a full bucket.
    assert await mw(_handler, FakeEvent(), _data(user_id=2)) == "handled"


@pytest.mark.asyncio
async def test_tokens_come_back_over_time():
    mw = middleware(rate=50.0, burst=1)

    assert await mw(_handler, FakeEvent(), _data()) == "handled"
    assert await mw(_handler, FakeEvent(), _data()) is None
    await asyncio.sleep(0.05)  # 50/s refills well within this
    assert await mw(_handler, FakeEvent(), _data()) == "handled"


@pytest.mark.asyncio
async def test_missing_user_is_never_throttled():
    """Channel posts and service events carry no user to throttle on."""
    mw = middleware(burst=1)
    for _ in range(5):
        assert await mw(_handler, FakeEvent(), {"event_from_user": None}) == "handled"


@pytest.mark.asyncio
async def test_admins_are_exempt():
    mw = middleware(burst=1, exempt=frozenset({7}))
    for _ in range(5):
        assert await mw(_handler, FakeEvent(), _data(user_id=7)) == "handled"


@pytest.mark.asyncio
async def test_throttled_events_are_counted():
    stats = ThrottleStats()
    inline = middleware(burst=1, kind="inline", notify=False, stats=stats)
    messages = middleware(burst=1, kind="message", stats=stats)

    await inline(_handler, FakeEvent(), _data())
    await inline(_handler, FakeEvent(), _data())
    await messages(_handler, FakeEvent(), _data(user_id=2))
    await messages(_handler, FakeEvent(), _data(user_id=2))

    assert stats.inline_throttled == 1
    assert stats.messages_throttled == 1
    assert "1 сообщений" in stats.as_text()


@pytest.mark.asyncio
async def test_warning_is_sent_once_not_per_message():
    mw = middleware(burst=1, notify=True)
    message = FakeMessage()

    await mw(_handler, message, _data())  # passes
    for _ in range(5):
        await mw(_handler, message, _data())  # all throttled

    assert len(message._replies) == 1
    assert "Слишком часто" in message._replies[0]


@pytest.mark.asyncio
async def test_inline_throttling_stays_silent():
    """An error row for one keystroke is worse than an empty dropdown."""
    mw = middleware(burst=1, kind="inline", notify=False)
    message = FakeMessage()

    await mw(_handler, message, _data())
    await mw(_handler, message, _data())

    assert message._replies == []


@pytest.mark.asyncio
async def test_bucket_store_is_bounded():
    mw = middleware(burst=1, max_users=10)
    for user_id in range(50):
        await mw(_handler, FakeEvent(), _data(user_id=user_id))
    assert len(mw._buckets) <= 10


# --- the overall search deadline -------------------------------------------


class SlowProvider(SearchProvider):
    name = "slow"

    def __init__(self, delay: float):
        super().__init__(rate=1000, burst=1000)
        self._delay = delay

    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        await asyncio.sleep(self._delay)
        return SearchPage(provider=self.name)


@pytest.mark.asyncio
async def test_search_gives_up_at_the_budget():
    """Per-provider timeouts stack; this is the single ceiling over all of them."""
    service = SearchService(
        [SlowProvider(delay=5.0)],
        MemoryCache(10, 1800),
        fresh_ttl=300,
        page_size=5,
        total_timeout=0.05,
    )

    with pytest.raises(SearchUnavailable) as exc:
        await service.search("anything")

    assert "budget" in str(exc.value)
    assert service.stats.timeouts == 1


@pytest.mark.asyncio
async def test_budget_overrun_still_falls_back_to_stale_cache():
    cache = MemoryCache(10, 1800)
    fast = SearchService([SlowProvider(delay=0.0)], cache, fresh_ttl=0, page_size=5)
    await fast.search("anything")  # populate

    slow = SearchService(
        [SlowProvider(delay=5.0)], cache, fresh_ttl=0, page_size=5, total_timeout=0.05
    )
    result = await slow.search("anything")

    assert result.source == "stale-cache"
