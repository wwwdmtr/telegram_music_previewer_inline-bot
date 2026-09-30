"""Per-user throttling.

Two surfaces with very different traffic shapes, so they get separate limits:

* **inline queries** fire while the user types — a dozen for one phrase is normal.
  Exceeding the limit drops the update silently: the keystroke shows nothing and
  the next one works. Answering with an error row would be far more jarring than
  a momentarily empty dropdown.
* **private messages** are deliberate, so a much lower limit fits, and there we
  do say something — once per cooldown, not on every throttled message.

The buckets live in a TTLCache: bounded by size and forgotten after idling, so a
flood from many user ids cannot grow memory without limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import InlineQuery, Message, TelegramObject, User
from cachetools import TTLCache

from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

# How long an idle user's bucket is remembered. Longer than any cooldown, short
# enough that one-off visitors do not linger.
BUCKET_TTL = 600


@dataclass
class ThrottleStats:
    messages_throttled: int = 0
    inline_throttled: int = 0

    def as_text(self) -> str:
        return (
            f"отброшено флудом: {self.messages_throttled} сообщений, "
            f"{self.inline_throttled} инлайн-запросов"
        )


class ThrottleMiddleware(BaseMiddleware):
    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        max_users: int,
        stats: ThrottleStats,
        kind: str,
        notify: bool,
        exempt: Optional[frozenset] = None,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._buckets: TTLCache = TTLCache(maxsize=max_users, ttl=BUCKET_TTL)
        # Remembers who has already been told off, so one warning is sent per
        # cooldown instead of one per throttled message.
        self._warned: TTLCache = TTLCache(maxsize=max_users, ttl=BUCKET_TTL)
        self._stats = stats
        self._kind = kind
        self._notify = notify
        self._exempt = exempt or frozenset()

    def _bucket(self, user_id: int) -> TokenBucket:
        bucket = self._buckets.get(user_id)
        if bucket is None:
            bucket = TokenBucket(rate=self._rate, capacity=self._burst)
            self._buckets[user_id] = bucket
        return bucket

    def _count(self) -> None:
        if self._kind == "inline":
            self._stats.inline_throttled += 1
        else:
            self._stats.messages_throttled += 1

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user")
        # No user (channel posts, service events) — nothing to throttle on.
        if user is None or user.id in self._exempt:
            return await handler(event, data)

        bucket = self._bucket(user.id)
        if bucket.try_acquire():
            return await handler(event, data)

        self._count()
        wait = bucket.seconds_until_free()
        log.info("throttled %s from user %s (%.1fs to go)", self._kind, user.id, wait)

        if self._notify and isinstance(event, Message) and user.id not in self._warned:
            self._warned[user.id] = True
            await event.answer(
                f"Слишком часто — подождите {max(1, round(wait))} с и повторите."
            )
        elif isinstance(event, InlineQuery):
            # Deliberately no answer: an empty dropdown for one keystroke is
            # less disruptive than an error row, and the next query gets through.
            pass
        return None
