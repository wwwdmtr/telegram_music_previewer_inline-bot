"""Query normalization and result de-duplication helpers."""

from __future__ import annotations

import re
from typing import Iterable

from .models import Track

MAX_QUERY_LENGTH = 100

_WHITESPACE = re.compile(r"\s+")
# Deezer documents a mini query language (artist:"x" track:"y"), but on the public
# endpoint it is unreliable — artist:"radiohead" returns nothing while plain
# "radiohead" works. So the bot accepts free-form text only and flattens those
# characters: left as-is they silently produce zero results.
_UPSTREAM_SPECIAL = re.compile(r'[":()\[\]]')
_NOT_ALNUM = re.compile(r"[^\w\s]", re.UNICODE)

# Markers that make one recording look like several different tracks. They are
# only ever stripped in positions where they cannot be part of the real name:
# every pattern below anchors on \b plus a bracket or a dash separator.
#
# Getting this wrong loses real results. An earlier version allowed the keywords
# to match bare and mid-word, which turned "Taylor Swift" into "taylor swi" (the
# "ft" of Swift), "Clean" into "" and made "Stay With Me" collide with "Stay".
_NOISE_WORDS = (
    r"feat\.?|ft\.?|with|prod\.?"
    r"|remaster(?:ed)?|remastered version|\d{4} remaster"
    r"|radio edit|album version|single version|extended|mono|stereo"
    r"|explicit|clean|bonus track"
)
# "(feat. X)", "[Remastered]", "(with JENNIE)" — anything bracketed that carries
# a marker. Safe because a bracketed group is never part of the plain name.
_BRACKETED_NOISE = re.compile(
    rf"\s*[(\[]\s*[^)\]]*\b(?:{_NOISE_WORDS})\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
# " - 2009 Remaster", " — Radio Edit": a dash-separated tail carrying a marker.
_DASHED_NOISE = re.compile(
    rf"\s+[-–—]\s*[^-–—]*\b(?:{_NOISE_WORDS})\b.*$",
    re.IGNORECASE,
)
# "Song feat. Somebody" — the one marker common enough to appear without
# brackets. Requires \b on both sides, so "Swift" survives.
_BARE_FEAT = re.compile(r"\s+\b(?:feat\.?|ft\.?|prod\.?)\s+.*$", re.IGNORECASE)


def normalize_query(query: str) -> str:
    """Cache key form: trimmed, collapsed, lowercased, length-capped."""
    return _WHITESPACE.sub(" ", query.strip()).lower()[:MAX_QUERY_LENGTH]


def sanitize_for_upstream(query: str) -> str:
    """Strip characters that would be interpreted by the provider's query DSL."""
    return _WHITESPACE.sub(" ", _UPSTREAM_SPECIAL.sub(" ", query)).strip()


def _strip_noise(value: str) -> str:
    stripped = _BRACKETED_NOISE.sub("", value)
    stripped = _DASHED_NOISE.sub("", stripped)
    stripped = _BARE_FEAT.sub("", stripped)
    # A title that consists only of a marker ("Clean", "Explicit") must keep its
    # own name — an empty key would collide with every other such title.
    return stripped if stripped.strip() else value


def _dedup_key(track: Track) -> tuple[str, str]:
    def clean(value: str) -> str:
        value = _strip_noise(value.lower())
        return _WHITESPACE.sub(" ", _NOT_ALNUM.sub(" ", value)).strip()

    return clean(track.artist), clean(track.title)


def dedupe(tracks: Iterable[Track]) -> list[Track]:
    """Drop repeats of the same recording, keeping the first (best-ranked) one."""
    seen: set[tuple[str, str]] = set()
    result: list[Track] = []
    for track in tracks:
        key = _dedup_key(track)
        if key in seen:
            continue
        seen.add(key)
        result.append(track)
    return result


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def shorten(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"
