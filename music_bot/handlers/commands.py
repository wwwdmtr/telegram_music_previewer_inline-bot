"""Everything outside inline mode: private chat, groups, being added somewhere.

The bot's real feature lives in inline mode, which people do not discover on
their own. So every non-inline surface exists to point at it: the buttons below
use switch_inline_query_*, which puts "@bot " straight into a chat's input field.
"""

from __future__ import annotations

import logging
from typing import Union

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import (
    JOIN_TRANSITION,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
)
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    SwitchInlineQueryChosenChat,
)

from ..config import Settings
from ..models import PREVIEW_DURATION
from ..render import caption_for, source_markup
from ..service import SearchService, SearchUnavailable
from ..throttle import ThrottleStats
from ..text import format_duration

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = (
    "🎵 <b>Поиск музыки прямо в чате</b>\n\n"
    "Напишите в любом чате <code>@{username} исполнитель название</code> — "
    "появится список, выберите нужный трек.\n\n"
    "Формат — обычный текст, порядок слов не важен:\n"
    "• <code>@{username} radiohead creep</code>\n"
    "• <code>@{username} creep radiohead</code>\n"
    "• <code>@{username} Radiohead — Creep</code>\n"
    "• <code>@{username} creep</code> — только название тоже работает\n\n"
    "Пустой запрос покажет текущий чарт.\n\n"
    "Можно просто прислать название сюда — отвечу лучшим совпадением.\n\n"
    "ℹ️ Доступны 30-секундные превью: это всё, что отдают открытые музыкальные API."
)

# Groups get a short version: nobody wants a wall of text after an add.
HELP_GROUP = (
    "🎵 Ищу музыку в инлайн-режиме.\n\n"
    "Наберите в этом чате <code>@{username} название трека</code> — "
    "появится список, тапом отправляется 30-секундное превью.\n\n"
    "Сообщения чата я не читаю."
)

NOT_FOUND_HINT = (
    "🔍 Ничего не нашлось.\n\n"
    "Попробуйте:\n"
    "• указать исполнителя и название: <code>Nirvana Lithium</code>\n"
    "• написать латиницей: <code>artik asti</code> вместо <code>артик и асти</code> — "
    "в каталоге многие исполнители значатся латиницей\n"
    "• указать название на языке оригинала, а не в переводе\n"
    "• проверить раскладку и опечатки"
)


def try_here_markup() -> InlineKeyboardMarkup:
    """Two ways into inline mode: right here, or in a chat of their choosing."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔎 Попробовать здесь", switch_inline_query_current_chat=""
                )
            ],
            [
                InlineKeyboardButton(
                    text="📨 Выбрать чат",
                    switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                        query="",
                        allow_user_chats=True,
                        allow_group_chats=True,
                        allow_channel_chats=False,
                    ),
                )
            ],
        ]
    )


async def _username(event: Union[Message, ChatMemberUpdated]) -> str:
    me = await event.bot.me()
    return me.username or "bot"


# --- private chat -----------------------------------------------------------


@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: Message, command: CommandObject) -> None:
    """Entry point for the inline "nothing found / error" buttons."""
    payload = (command.args or "").strip()
    if payload == "notfound":
        await message.answer(NOT_FOUND_HINT, reply_markup=try_here_markup())
        return
    if payload == "error":
        await message.answer(
            "⚠️ Музыкальный сервис временно не отвечает. "
            "Обычно это проходит за пару минут — попробуйте ещё раз."
        )
        return
    await start(message)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        HELP.format(username=await _username(message)), reply_markup=try_here_markup()
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    username = await _username(message)
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(HELP.format(username=username), reply_markup=try_here_markup())
        return
    # In a group the button opens inline mode in that same group — which is
    # exactly the thing we want to demonstrate.
    await message.answer(HELP_GROUP.format(username=username), reply_markup=try_here_markup())


@router.message(Command("stats"))
async def stats_command(
    message: Message,
    search: SearchService,
    settings: Settings,
    throttle_stats: ThrottleStats,
) -> None:
    user = message.from_user
    if not settings.admin_ids or user is None or user.id not in settings.admin_ids:
        return
    body = f"{search.stats.as_text()}\n{throttle_stats.as_text()}"
    await message.answer(f"<b>Статистика</b>\n<pre>{body}</pre>")


# Private chats only. Without the filter, a bot added to a group with privacy
# mode off would try to search every single message posted there.
@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def search_in_chat(message: Message, search: SearchService, settings: Settings) -> None:
    """Convenience: a plain text message in PM returns the top match as audio."""
    query = message.text.strip()
    if len(query) < settings.min_query_length:
        await message.answer(
            f"Слишком короткий запрос — минимум {settings.min_query_length} символа."
        )
        return

    try:
        result = await search.search(query)
    except SearchUnavailable:
        await message.answer("⚠️ Поиск сейчас недоступен, попробуйте через минуту.")
        return

    if not result.page.tracks:
        await message.answer(NOT_FOUND_HINT, reply_markup=try_here_markup())
        return

    track = result.page.tracks[0]
    caption = caption_for(track, settings.caption_style)
    await message.answer_audio(
        audio=track.preview_url,
        title=track.title,
        performer=track.artist,
        duration=PREVIEW_DURATION,
        caption=caption,
        parse_mode="HTML" if caption else None,
        reply_markup=source_markup(track, settings.show_source_button),
    )
    if len(result.page.tracks) > 1:
        others = "\n".join(
            f"• {t.artist} — {t.title} ({format_duration(t.duration)})"
            for t in result.page.tracks[1:6]
        )
        await message.answer(
            f"Ещё варианты — выбрать можно в инлайн-режиме:\n{others}",
            parse_mode=None,
            reply_markup=try_here_markup(),
        )


@router.message(F.chat.type == ChatType.PRIVATE)
async def unsupported_message(message: Message) -> None:
    """Sticker, photo, voice… — say what to do instead of staying silent."""
    await message.answer(
        "Я ищу музыку по тексту: пришлите название трека или исполнителя.",
        reply_markup=try_here_markup(),
    )


# --- groups -----------------------------------------------------------------


@router.my_chat_member(
    ChatMemberUpdatedFilter(JOIN_TRANSITION),
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def added_to_group(event: ChatMemberUpdated) -> None:
    """Without this, the bot gets added to a chat and does nothing visible."""
    log.info("added to chat %s (%s)", event.chat.id, event.chat.title)
    try:
        await event.answer(
            HELP_GROUP.format(username=await _username(event)),
            reply_markup=try_here_markup(),
        )
    except TelegramForbiddenError:
        # Added with sending restricted: nothing to do, and not an error.
        log.info("cannot post in chat %s", event.chat.id)
