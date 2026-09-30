"""Server-side debounce for inline queries.

Telegram decides when to send an inline_query — typing "radiohead" produces a
query per keystroke, each a different string, so the cache cannot help: nine
prefixes mean nine upstream calls. There is no way to debounce on the client.

What we can do is wait a moment before searching and abandon the query if a
newer keystroke from the same user arrived in the meantime. Only the last query
of a burst reaches the provider. Leaving an inline query unanswered is harmless:
the client keeps showing the previous list and the next query answers.

Two deliberate limits:

* Only first-page queries are debounced. A paginated request (offset > 0) comes
  from scrolling, not typing, and Telegram does not retry it — delaying or
  dropping one would break "load more".
* Anything already in the cache skips the wait entirely, so the debounce never
  makes a fast answer slow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from cachetools import TTLCache

log = logging.getLogger(__name__)

SEQ_TTL = 60


class Debouncer:
    def __init__(self, delay: float, max_users: int, stats: Optional[object] = None) -> None:
        self._delay = delay
        # user id -> sequence number of their most recent query
        self._latest: TTLCache = TTLCache(maxsize=max_users, ttl=SEQ_TTL)
        self._stats = stats

    @property
    def delay(self) -> float:
        return self._delay

    async def settle(self, user_id: int) -> bool:
        """Wait out the debounce window.

        Returns False if a newer query from this user appeared while waiting —
        the caller should then answer nothing at all.
        """
        if self._delay <= 0:
            return True

        seq = self._latest.get(user_id, 0) + 1
        self._latest[user_id] = seq

        await asyncio.sleep(self._delay)

        if self._latest.get(user_id) != seq:
            if self._stats is not None:
                self._stats.debounced += 1
            log.debug("query %d from user %s superseded", seq, user_id)
            return False
        return True
