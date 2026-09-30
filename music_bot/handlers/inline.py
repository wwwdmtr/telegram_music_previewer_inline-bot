"""inline_query handling — the core of the bot."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Router
from aiogram.types import ChosenInlineResult, InlineQuery, InlineQueryResultsButton

from ..config import Settings
from ..debounce import Debouncer
from ..render import audio_results, notice_result
from ..service import SearchService, SearchUnavailable
from ..text import shorten

log = logging.getLogger(__name__)
router = Router(name="inline")


# Deezer stops returning useful results long before this; past it we simply end
# the list. Clamping instead would re-serve the same page forever, because the
# next_offset we hand back would get clamped to the same value on the way in.
MAX_OFFSET = 500


def _parse_offset(raw: str) -> int:
    """Telegram gives back whatever string we sent as next_offset."""
    try:
        offset = int(raw or 0)
    except ValueError:
        return 0
    return max(0, offset)


@router.inline_query()
async def handle_inline(
    query: InlineQuery,
    search: SearchService,
    settings: Settings,
    debouncer: Debouncer,
) -> None:
    text = query.query.strip()
    offset = _parse_offset(query.offset)

    # 1. Too short to be meaningful — do not spend an upstream call on it.
    if text and len(text) < settings.min_query_length:
        await query.answer(
            results=[],
            cache_time=settings.inline_cache_time,
            is_personal=False,
            button=InlineQueryResultsButton(
                text=f"Введите минимум {settings.min_query_length} символа",
                start_parameter="help",
            ),
        )
        return

    # 2. Scrolled past what the upstream can usefully paginate — end the list.
    if offset >= MAX_OFFSET:
        await query.answer(results=[], cache_time=settings.inline_cache_time, is_personal=False)
        return

    # 3. Already cached — answer now, no reason to sit through the debounce.
    cached = await search.peek(text, offset)
    if cached is None:
        # Typing produces one query per keystroke. Wait briefly and drop this
        # one if a newer keystroke arrives, so only the last prefix of a burst
        # reaches the provider. Pagination is exempt: it comes from scrolling,
        # and Telegram does not retry a request we leave unanswered.
        if offset == 0 and query.from_user is not None:
            if not await debouncer.settle(query.from_user.id):
                return

    try:
        # 4. Empty query — show the Deezer chart instead of nothing.
        result = cached or await (search.chart() if not text else search.search(text, offset))
    except SearchUnavailable as exc:
        log.warning("search unavailable for %r: %s", text, exc)
        await query.answer(
            results=[
                notice_result(
                    title="Сервис поиска недоступен",
                    description="Попробуйте повторить запрос через минуту",
                    message="⚠️ Поиск музыки сейчас недоступен, попробуйте позже.",
                )
            ],
            # Short cache time: the failure must not be frozen for 5 minutes.
            cache_time=settings.error_cache_time,
            is_personal=False,
            button=InlineQueryResultsButton(
                text="Сервис недоступен — подробнее", start_parameter="error"
            ),
        )
        return

    tracks = result.page.tracks

    # 5. Nothing found.
    if not tracks:
        # On a second page an empty result simply means the list ended — no need
        # to shout "nothing found" at the user.
        if offset:
            await query.answer(results=[], cache_time=settings.inline_cache_time, is_personal=False)
            return
        await query.answer(
            results=[
                notice_result(
                    title="Ничего не найдено",
                    # description is plain text; message_text is parsed as HTML,
                    # so the raw query must be escaped there. Unescaped, a query
                    # containing "<" makes answerInlineQuery fail outright and
                    # the user sees nothing at all.
                    description=f"По запросу «{shorten(text, 40)}» нет результатов",
                    message=f"🔍 По запросу «{escape(shorten(text, 60))}» ничего не нашлось.",
                )
            ],
            cache_time=settings.inline_cache_time,
            is_personal=False,
            button=InlineQueryResultsButton(
                text="Ничего не найдено — как искать?", start_parameter="notfound"
            ),
        )
        return

    # 6. Normal path. next_offset advances by the requested page size (the
    # upstream index), not by the number of rows left after de-duplication.
    next_page = offset + search.page_size
    next_offset = str(next_page) if result.page.has_more and next_page < MAX_OFFSET else ""

    await query.answer(
        results=audio_results(
            tracks,
            source_button=settings.show_source_button,
            caption_style=settings.caption_style,
        ),
        cache_time=settings.inline_cache_time,
        # Results depend only on the query, so let Telegram's edge cache serve
        # the same answer to every user.
        is_personal=False,
        next_offset=next_offset,
    )
    log.info(
        "inline q=%r offset=%s -> %d tracks (source=%s)",
        text,
        offset,
        len(tracks),
        result.source,
    )


@router.chosen_inline_result()
async def handle_chosen(chosen: ChosenInlineResult) -> None:
    """Only for metrics: which queries actually convert into a sent track."""
    log.info("chosen result=%s query=%r", chosen.result_id, chosen.query)
