"""Settings parsing — the paths that only ever run on a real deploy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from music_bot.config import Settings

# A syntactically valid stand-in: Settings now rejects malformed tokens.
TOKEN = "123456789:AAEdummytokenfortestsonly0123456789"


def load(env: dict[str, str], monkeypatch) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # _env_file=None: never read a developer's real .env during tests.
    return Settings(_env_file=None)


def test_defaults(monkeypatch):
    settings = load({"BOT_TOKEN": TOKEN}, monkeypatch)
    assert settings.providers == ["deezer", "itunes"]
    assert settings.admin_ids == []


def test_empty_admin_ids_is_not_an_error(monkeypatch):
    """.env.example ships ADMIN_IDS= empty; this must not crash on startup."""
    settings = load({"BOT_TOKEN": TOKEN, "ADMIN_IDS": ""}, monkeypatch)
    assert settings.admin_ids == []


def test_comma_separated_lists(monkeypatch):
    settings = load(
        {"BOT_TOKEN": TOKEN, "PROVIDERS": "itunes, deezer", "ADMIN_IDS": "42, 100500"},
        monkeypatch,
    )
    assert settings.providers == ["itunes", "deezer"]  # order = fallback order
    assert settings.admin_ids == [42, 100500]


def test_single_provider(monkeypatch):
    settings = load({"BOT_TOKEN": TOKEN, "PROVIDERS": "deezer"}, monkeypatch)
    assert settings.providers == ["deezer"]


def test_missing_token_is_a_validation_error(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert exc.value.errors()[0]["loc"] == ("bot_token",)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"PROVIDERS": "spotify"}, "unknown providers"),
        ({"PROVIDERS": ""}, "at least one provider"),
        ({"ADMIN_IDS": "not-a-number"}, "not a Telegram user id"),
        ({"PAGE_SIZE": "100"}, "between 1 and 50"),
        ({"PAGE_SIZE": "0"}, "between 1 and 50"),
        ({"CACHE_BACKEND": "memcached"}, "cache_backend"),
        ({"CACHE_FRESH_TTL": "600", "CACHE_STALE_TTL": "300"}, "must be >="),
    ],
)
def test_bad_values_are_rejected(env, expected, monkeypatch):
    with pytest.raises(ValidationError) as exc:
        load({"BOT_TOKEN": TOKEN, **env}, monkeypatch)
    assert expected in str(exc.value)


def test_env_file_is_read(tmp_path, monkeypatch):
    """The Docker path: values come from a file, not the process environment."""
    for key in ("BOT_TOKEN", "ADMIN_IDS", "PROVIDERS", "CACHE_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"BOT_TOKEN={TOKEN}\n"
        "ADMIN_IDS=\n"  # empty value in a file — the case that used to break
        "PROVIDERS=deezer,itunes\n"
        "CACHE_BACKEND=redis\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=str(env_file))
    assert settings.bot_token == TOKEN
    assert settings.admin_ids == []
    assert settings.providers == ["deezer", "itunes"]
    assert settings.cache_backend == "redis"


def test_env_overrides_env_file(tmp_path, monkeypatch):
    """The Redis overlay relies on this: `environment:` beats `env_file:`."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"BOT_TOKEN={TOKEN}\nCACHE_BACKEND=memory\n", encoding="utf-8")
    monkeypatch.setenv("CACHE_BACKEND", "redis")
    settings = Settings(_env_file=str(env_file))
    assert settings.cache_backend == "redis"


def test_alias_fields_are_set_by_env_name(monkeypatch):
    """ADMIN_IDS/PROVIDERS carry an alias, so the alias is the way to set them.

    Passing the field name instead is silently ignored in favour of the .env
    value — a trap worth pinning down rather than rediscovering.
    """
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    assert Settings(bot_token=TOKEN, ADMIN_IDS="42", _env_file=None).admin_ids == [42]
    assert Settings(bot_token=TOKEN, PROVIDERS="itunes", _env_file=None).providers == ["itunes"]


@pytest.mark.parametrize(
    "token, reason",
    [
        ("123456789:AAEabcdefghijklmnopqrstuvwxyz0123456 A", "пробел"),
        ('"123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"', "кавычках"),
        ("nocolon", "двоеточия"),
        ("123456789:short", "ожидается вид"),
    ],
)
def test_malformed_token_is_explained(token, reason, monkeypatch):
    """A stray space in a pasted token used to surface as an aiogram traceback."""
    with pytest.raises(ValidationError) as exc:
        load({"BOT_TOKEN": token}, monkeypatch)
    assert reason in str(exc.value)


def test_surrounding_whitespace_in_token_is_trimmed(monkeypatch):
    good = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
    assert load({"BOT_TOKEN": f"  {good}\t"}, monkeypatch).bot_token == good
