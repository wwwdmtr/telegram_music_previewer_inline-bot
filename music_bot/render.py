"""Turning tracks into Telegram inline results.

Note on visuals: unlike @pic, `InlineQueryResultAudio` has no thumbnail field in
the Bot API, so album art cannot be shown in the dropdown — Telegram draws its
own audio icon. The cover is still useful in the article-style placeholders and
in the message the user ends up sending.
"""

from __future__ import annotations

import hashlib
from html import escape
from typing import List, Optional

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InputTextMessageContent,
)

from .models import PREVIEW_DURATION, Track
from .text import format_duration, shorten

# Telegram caps result ids at 64 bytes.
_ID_LENGTH = 32
# Titles longer than this get cut by clients anyway.
_TITLE_LIMIT = 64


def result_id(track: Track) -> str:
    return hashlib.sha1(track.uid.encode("utf-8")).hexdigest()[:_ID_LENGTH]


def caption_for(track: Track, style: str = "full") -> Optional[str]:
    """HTML caption of the message the user sends. None means no caption."""
    if style == "none":
        return None
    head = f"🎵 <b>{escape(track.title)}</b> — {escape(track.artist)}"
    if style == "short":
        return head
    meta = []
    if track.album:
        meta.append(escape(shorten(track.album, 40)))
    if track.duration:
        meta.append(f"полная версия {format_duration(track.duration)}")
    meta.append("превью 30 сек")
    return head + "\n<i>" + " · ".join(meta) + "</i>"


def source_markup(track: Track, enabled: bool) -> Optional[InlineKeyboardMarkup]:
    if not enabled or not track.source_url:
        return None
    label = "Открыть в Deezer" if track.provider == "deezer" else "Открыть в Apple Music"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, url=track.source_url)]]
    )


def audio_result(
    track: Track, *, source_button: bool = True, caption_style: str = "full"
) -> InlineQueryResultAudio:
    title = shorten(track.title, _TITLE_LIMIT)
    if track.explicit:
        title = f"{title} 🔞"
    caption = caption_for(track, caption_style)
    return InlineQueryResultAudio(
        id=result_id(track),
        audio_url=track.preview_url,
        # title/performer drive both the dropdown row and the audio player.
        title=title,
        performer=shorten(track.artist, _TITLE_LIMIT),
        # Must describe the linked file (a 30 s preview), not the original track.
        audio_duration=PREVIEW_DURATION,
        caption=caption,
        parse_mode="HTML" if caption else None,
        reply_markup=source_markup(track, source_button),
    )


def audio_results(
    tracks: List[Track], *, source_button: bool = True, caption_style: str = "full"
) -> List[InlineQueryResultAudio]:
    return [
        audio_result(t, source_button=source_button, caption_style=caption_style) for t in tracks
    ]


def notice_result(
    *, title: str, description: str, message: str, thumb_url: str = ""
) -> InlineQueryResultArticle:
    """A single visible row explaining what happened.

    Used when an empty result list would just show nothing in some clients.
    """
    return InlineQueryResultArticle(
        id=hashlib.sha1(f"notice:{title}:{description}".encode("utf-8")).hexdigest()[:_ID_LENGTH],
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=message, parse_mode="HTML", link_preview_options=None
        ),
        thumbnail_url=thumb_url or None,
    )
