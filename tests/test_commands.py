"""Non-inline surfaces: private chat, groups, being added to a chat."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest
from aiogram.enums import ChatType

from music_bot.cache import MemoryCache
from music_bot.config import Settings
from music_bot.handlers import commands
from music_bot.handlers.commands import (
    added_to_group,
    help_command,
    search_in_chat,
    start,
    stats_command,
    try_here_markup,
    unsupported_message,
)
from music_bot.models import SearchPage, Track
from music_bot.providers.base import ProviderError, SearchProvider
from music_bot.service import SearchService
from music_bot.throttle import ThrottleStats

# A syntactically valid stand-in: Settings now rejects malformed tokens.
TOKEN = "123456789:AAEdummytokenfortestsonly0123456789"


@dataclass
class FakeMe:
    username: str = "musicbot"


class FakeBot:
    async def me(self):
        return FakeMe()


@dataclass
class FakeChat:
    type: str = ChatType.PRIVATE
    id: int = 1
    title: Optional[str] = None


@dataclass
class FakeUser:
    id: int = 42


@dataclass
class FakeMessage:
    """Only the attributes the handlers actually touch."""

    text: Optional[str] = None
    chat: FakeChat = field(default_factory=FakeChat)
    from_user: Optional[FakeUser] = field(default_factory=FakeUser)
    bot: FakeBot = field(default_factory=FakeBot)
    sent: List[dict] = field(default_factory=list)

    async def answer(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})

    async def answer_audio(self, audio, **kwargs):
        self.sent.append({"audio": audio, **kwargs})


@dataclass
class FakeChatMemberUpdated:
    chat: FakeChat = field(default_factory=lambda: FakeChat(type=ChatType.SUPERGROUP, title="Chat"))
    bot: FakeBot = field(default_factory=FakeBot)
    sent: List[dict] = field(default_factory=list)

    async def answer(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})


class StubProvider(SearchProvider):
    name = "stub"

    def __init__(self, tracks=None, fail=False):
        super().__init__(rate=1000, burst=1000)
        self._tracks = tracks or []
        self._fail = fail

    async def search(self, query: str, offset: int, limit: int) -> SearchPage:
        if self._fail:
            raise ProviderError("down")
        return SearchPage(tracks=list(self._tracks), provider=self.name)


def track(n: int = 1) -> Track:
    return Track(
        id=str(n),
        title=f"Song {n}",
        artist=f"Artist {n}",
        duration=200,
        preview_url=f"https://cdn/{n}.mp3",
        provider="deezer",
        source_url=f"https://deezer.com/{n}",
    )


def settings(**over) -> Settings:
    base = dict(bot_token=TOKEN, min_query_length=2)
    base.update(over)
    return Settings(**base)


def service(provider) -> SearchService:
    return SearchService([provider], MemoryCache(100, 1800), fresh_ttl=300, page_size=5)


# --- the inline hand-off ----------------------------------------------------


def test_buttons_hand_the_user_into_inline_mode():
    """The whole point of the non-inline surface: get people into inline mode."""
    rows = try_here_markup().inline_keyboard
    here = rows[0][0]
    chosen = rows[1][0]
    # An empty string (not None) is what opens inline mode with a blank query.
    assert here.switch_inline_query_current_chat == ""
    assert chosen.switch_inline_query_chosen_chat.allow_group_chats is True
    assert chosen.switch_inline_query_chosen_chat.allow_channel_chats is False


@pytest.mark.asyncio
async def test_start_explains_and_offers_the_button():
    message = FakeMessage(text="/start")
    await start(message)

    sent = message.sent[0]
    assert "@musicbot" in sent["text"]
    assert sent["reply_markup"] is not None


# --- groups -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_in_a_group_is_the_short_version():
    private = FakeMessage(text="/help", chat=FakeChat(type=ChatType.PRIVATE))
    group = FakeMessage(text="/help", chat=FakeChat(type=ChatType.SUPERGROUP))

    await help_command(private)
    await help_command(group)

    assert len(group.sent[0]["text"]) < len(private.sent[0]["text"])
    assert "Сообщения чата я не читаю" in group.sent[0]["text"]


@pytest.mark.asyncio
async def test_being_added_to_a_group_produces_a_greeting():
    event = FakeChatMemberUpdated()
    await added_to_group(event)

    assert "@musicbot" in event.sent[0]["text"]
    assert event.sent[0]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_greeting_survives_a_chat_that_forbids_posting():
    from aiogram.exceptions import TelegramForbiddenError

    class Muted(FakeChatMemberUpdated):
        async def answer(self, text, **kwargs):
            raise TelegramForbiddenError(method=None, message="not enough rights")

    await added_to_group(Muted())  # must not raise


# --- private chat search ----------------------------------------------------


@pytest.mark.asyncio
async def test_plain_text_returns_audio_and_alternatives():
    tracks = [track(1), track(2), track(3)]
    message = FakeMessage(text="nirvana")

    await search_in_chat(message, service(StubProvider(tracks)), settings())

    audio, alternatives = message.sent
    assert audio["audio"] == "https://cdn/1.mp3"
    assert audio["duration"] == 30  # the preview, not the full track
    assert "Song 2" in alternatives["text"] and "Song 3" in alternatives["text"]


@pytest.mark.asyncio
async def test_single_result_sends_no_alternatives_message():
    message = FakeMessage(text="nirvana")
    await search_in_chat(message, service(StubProvider([track(1)])), settings())
    assert len(message.sent) == 1


@pytest.mark.asyncio
async def test_too_short_text_is_rejected_without_searching():
    provider = StubProvider([track(1)])
    message = FakeMessage(text="a")

    await search_in_chat(message, service(provider), settings())

    assert "минимум 2" in message.sent[0]["text"]
    assert "audio" not in message.sent[0]


@pytest.mark.asyncio
async def test_nothing_found_gives_actionable_advice():
    message = FakeMessage(text="asdkjhqwe")
    await search_in_chat(message, service(StubProvider([])), settings())
    assert "латиницей" in message.sent[0]["text"]


@pytest.mark.asyncio
async def test_upstream_down_is_reported_not_raised():
    message = FakeMessage(text="nirvana")
    await search_in_chat(message, service(StubProvider(fail=True)), settings())
    assert "недоступен" in message.sent[0]["text"]


@pytest.mark.asyncio
async def test_non_text_message_gets_an_explanation():
    """A sticker or a voice note used to be answered with silence."""
    message = FakeMessage(text=None)
    await unsupported_message(message)
    assert "текст" in message.sent[0]["text"].lower()


# --- /stats -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_is_silent_for_non_admins():
    message = FakeMessage(text="/stats", from_user=FakeUser(id=999))
    await stats_command(message, service(StubProvider()), settings(ADMIN_IDS="42"), ThrottleStats())
    assert message.sent == []


@pytest.mark.asyncio
async def test_stats_answers_an_admin():
    message = FakeMessage(text="/stats", from_user=FakeUser(id=42))
    await stats_command(message, service(StubProvider()), settings(ADMIN_IDS="42"), ThrottleStats())
    assert "hit-rate" in message.sent[0]["text"]


@pytest.mark.asyncio
async def test_stats_without_from_user_does_not_crash():
    """Posts sent on behalf of a channel carry no from_user."""
    message = FakeMessage(text="/stats", from_user=None)
    await stats_command(message, service(StubProvider()), settings(ADMIN_IDS="42"), ThrottleStats())
    assert message.sent == []


def test_help_text_has_no_unfilled_placeholders():
    filled = commands.HELP.format(username="musicbot")
    assert "{" not in filled and "}" not in filled
