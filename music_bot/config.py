"""Settings, read from environment / .env (see .env.example)."""

from __future__ import annotations

import re
from typing import List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KNOWN_PROVIDERS = ("deezer", "itunes")

# <bot id>:<secret>. Catching a malformed token here turns aiogram's traceback
# into one readable line — pasting it with a stray space is an easy slip.
_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Telegram ---------------------------------------------------------
    bot_token: str = Field(..., description="Token from @BotFather")

    # Comma-separated lists are kept as plain strings on purpose: for List[...]
    # fields pydantic-settings tries to JSON-decode the raw env value inside the
    # source, before any validator runs, so "a,b" — and even an empty value —
    # would blow up with SettingsError. Parsing happens in the properties below.
    #
    # These two carry an alias, so they must be set by the alias — Settings(
    # admin_ids_raw="42") is silently overridden by the .env value, while
    # Settings(ADMIN_IDS="42") works. test_alias_fields_are_set_by_env_name
    # pins that down.
    admin_ids_raw: str = Field("", alias="ADMIN_IDS", description="Who may call /stats")
    providers_raw: str = Field(
        ",".join(KNOWN_PROVIDERS),
        alias="PROVIDERS",
        description="Fallback order: the first provider that answers wins",
    )

    # --- Providers --------------------------------------------------------
    upstream_timeout: float = 2.0
    # Hard ceiling for one search, fallbacks included. Without it the worst case
    # is (bucket wait + HTTP) per provider — about 8 s for two providers, which
    # is exactly what "the bot hangs" looks like from the user's side.
    search_timeout: float = 4.0
    # Deezer tolerates ~50 req / 5 s per IP; stay comfortably under it.
    deezer_rate: float = 8.0
    deezer_burst: int = 10
    # iTunes documents ~20 req / min.
    itunes_rate: float = 0.3
    itunes_burst: int = 5

    # --- Cache ------------------------------------------------------------
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    cache_fresh_ttl: int = 300  # 5 min — the window the task asked for
    cache_stale_ttl: int = 1800  # kept beyond freshness purely as a failure fallback
    cache_max_entries: int = 5000  # memory backend only

    # --- Inline behaviour -------------------------------------------------
    page_size: int = 20  # results per page (Telegram allows up to 50)
    min_query_length: int = 2
    # How long Telegram itself may reuse our answer for the same query. Results
    # are identical for every user, so this is the cheapest cache layer we have.
    inline_cache_time: int = 300
    error_cache_time: int = 5  # never let Telegram cache a failure for long

    # --- What the sent message looks like ---------------------------------
    # "full"  — title, artist, album, full length, "preview 30 s"
    # "short" — title and artist only
    # "none"  — no caption at all: just the audio player, which already shows
    #           the title and performer we pass in the result
    caption_style: Literal["full", "short", "none"] = "full"
    show_source_button: bool = True

    # --- Per-user throttling ---------------------------------------------
    # Inline queries fire per keystroke, so the allowance is deliberately wide:
    # burst covers one typed phrase, rate sustains continued typing.
    throttle_inline_rate: float = 2.0
    throttle_inline_burst: int = 10
    # Private messages are deliberate acts — a much lower limit is plenty.
    throttle_message_rate: float = 0.5
    throttle_message_burst: int = 5
    # Upper bound on remembered users, so a flood of ids cannot grow memory.
    throttle_max_users: int = 10000

    log_level: str = "INFO"

    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        # Surrounding whitespace is a harmless paste artefact — trim it.
        token = value.strip()
        if _BOT_TOKEN_RE.match(token):
            return token
        if " " in token:
            reason = "внутри значения есть пробел"
        elif token.startswith(('"', "'")):
            reason = "значение в кавычках — в .env они не нужны"
        elif ":" not in token:
            reason = "нет двоеточия"
        else:
            reason = "ожидается вид 123456789:AAE...-xyz"
        raise ValueError(f"не похоже на токен от @BotFather: {reason}")

    @staticmethod
    def _split(raw: str) -> List[str]:
        return [part.strip() for part in raw.split(",") if part.strip()]

    @property
    def providers(self) -> List[str]:
        return self._split(self.providers_raw)

    @property
    def admin_ids(self) -> List[int]:
        return [int(part) for part in self._split(self.admin_ids_raw)]

    @field_validator("providers_raw")
    @classmethod
    def _check_providers(cls, value: str) -> str:
        names = cls._split(value)
        if not names:
            raise ValueError("at least one provider must be enabled")
        unknown = [n for n in names if n not in KNOWN_PROVIDERS]
        if unknown:
            raise ValueError(
                f"unknown providers: {', '.join(unknown)}; known: {', '.join(KNOWN_PROVIDERS)}"
            )
        return value

    @field_validator("admin_ids_raw")
    @classmethod
    def _check_admin_ids(cls, value: str) -> str:
        for part in cls._split(value):
            if not part.lstrip("-").isdigit():
                raise ValueError(f"{part!r} is not a Telegram user id (expected digits)")
        return value

    @field_validator("page_size")
    @classmethod
    def _telegram_limit(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("must be between 1 and 50 (Telegram's inline limit)")
        return value

    @model_validator(mode="after")
    def _check_timeouts(self) -> "Settings":
        if self.search_timeout < self.upstream_timeout:
            raise ValueError(
                "SEARCH_TIMEOUT must be >= UPSTREAM_TIMEOUT, otherwise no single "
                "provider call can ever finish"
            )
        return self

    @model_validator(mode="after")
    def _check_ttls(self) -> "Settings":
        if self.cache_stale_ttl < self.cache_fresh_ttl:
            raise ValueError(
                "CACHE_STALE_TTL must be >= CACHE_FRESH_TTL: an entry cannot stop "
                "existing before it stops being fresh"
            )
        return self
