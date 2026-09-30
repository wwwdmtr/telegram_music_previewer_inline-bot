"""Entrypoint: wiring and long-polling loop."""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from pydantic import ValidationError

from .cache import CacheBackend, MemoryCache, RedisCache
from .config import Settings
from .handlers import build_router
from .providers import DeezerProvider, ITunesProvider, SearchProvider
from .service import SearchService
from .throttle import ThrottleMiddleware, ThrottleStats

log = logging.getLogger("music_bot")


def build_cache(settings: Settings) -> CacheBackend:
    if settings.cache_backend == "redis":
        log.info("cache: redis at %s", settings.redis_url)
        return RedisCache(settings.redis_url)
    log.info("cache: in-process (max %d entries)", settings.cache_max_entries)
    return MemoryCache(max_entries=settings.cache_max_entries, ttl=settings.cache_stale_ttl)


def build_providers(settings: Settings, client: httpx.AsyncClient) -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    for name in settings.providers:
        if name == "deezer":
            providers.append(
                DeezerProvider(
                    client,
                    rate=settings.deezer_rate,
                    burst=settings.deezer_burst,
                    timeout=settings.upstream_timeout,
                )
            )
        elif name == "itunes":
            providers.append(
                ITunesProvider(
                    client,
                    rate=settings.itunes_rate,
                    burst=settings.itunes_burst,
                    timeout=settings.upstream_timeout,
                )
            )
    log.info("providers: %s", ", ".join(p.name for p in providers))
    return providers


def load_settings() -> Settings:
    """Config errors are the most common deploy problem — report them in one line."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        print(
            "Ошибка конфигурации:\n"
            f"{problems}\n\n"
            "Задайте переменные окружения — локально через .env (шаблон — "
            ".env.example), на хостинге через панель. Токен BOT_TOKEN выдаёт @BotFather.",
            file=sys.stderr,
        )
        raise SystemExit(2)


async def publish_commands(bot: Bot) -> None:
    """Fill the "/" menu. Without this the UI offers no hint at all.

    Idempotent, so it is safe to call on every start.
    """
    private = [
        BotCommand(command="start", description="Как пользоваться"),
        BotCommand(command="help", description="Помощь и примеры запросов"),
    ]
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
    # In groups the bot only answers commands addressed to it; keep the menu to
    # the one command that makes sense there.
    await bot.set_my_commands(
        [BotCommand(command="help", description="Как искать музыку в этом чате")],
        scope=BotCommandScopeAllGroupChats(),
    )


async def run(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    cache = build_cache(settings)
    http = httpx.AsyncClient(
        timeout=settings.upstream_timeout,
        headers={"User-Agent": "music-inline-bot/1.0"},
        # Keep-alive matters: a fresh TLS handshake per keystroke would blow the
        # inline-query latency budget on its own.
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        follow_redirects=True,
    )
    service = SearchService(
        providers=build_providers(settings, http),
        cache=cache,
        fresh_ttl=settings.cache_fresh_ttl,
        stale_ttl=settings.cache_stale_ttl,
        page_size=settings.page_size,
        total_timeout=settings.search_timeout,
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp["search"] = service
    dp["settings"] = settings
    throttle_stats = ThrottleStats()
    dp["throttle_stats"] = throttle_stats
    dp.include_router(build_router())

    # Outer middleware: runs before filters, so a throttled update costs nothing.
    # Admins are exempt so /stats keeps working while a flood is going on.
    exempt = frozenset(settings.admin_ids)
    dp.inline_query.outer_middleware(
        ThrottleMiddleware(
            rate=settings.throttle_inline_rate,
            burst=settings.throttle_inline_burst,
            max_users=settings.throttle_max_users,
            stats=throttle_stats,
            kind="inline",
            notify=False,
            exempt=exempt,
        )
    )
    dp.message.outer_middleware(
        ThrottleMiddleware(
            rate=settings.throttle_message_rate,
            burst=settings.throttle_message_burst,
            max_users=settings.throttle_max_users,
            stats=throttle_stats,
            kind="message",
            notify=True,
            exempt=exempt,
        )
    )
    log.info(
        "throttle: inline %.1f/s burst %d, messages %.1f/s burst %d",
        settings.throttle_inline_rate,
        settings.throttle_inline_burst,
        settings.throttle_message_rate,
        settings.throttle_message_burst,
    )

    try:
        try:
            me = await bot.me()
        except TelegramUnauthorizedError:
            log.critical(
                "Telegram отверг токен. Проверьте BOT_TOKEN (.env или переменные "
                "окружения в панели хостинга) — его выдаёт @BotFather."
            )
            raise SystemExit(2)
        log.info("starting as @%s (id=%s)", me.username, me.id)
        if not me.supports_inline_queries:
            log.warning(
                "inline mode is OFF for @%s — run /setinline in @BotFather, "
                "otherwise the bot will not answer in chats at all",
                me.username,
            )
        await publish_commands(bot)
        # Drop anything queued while the bot was offline: stale inline queries
        # cannot be answered anyway (their ids have expired).
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await http.aclose()
        await cache.close()
        await bot.session.close()
        log.info("stopped")


def main() -> None:
    settings = load_settings()
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    # SystemExit is a BaseException: it passes through the guard below untouched.
    except Exception as exc:  # noqa: BLE001 - top level guard, log then exit non-zero
        logging.getLogger("music_bot").critical("fatal: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
