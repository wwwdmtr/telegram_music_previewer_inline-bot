"""Domain models shared between providers, cache and handlers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Both Deezer and iTunes hand out 30-second previews, no matter how long the
# original track is. Telegram trusts whatever we put into `audio_duration`, so
# it has to describe the file we actually link to, not the full song.
PREVIEW_DURATION = 30


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    duration: int
    preview_url: str
    provider: str
    album: str = ""
    cover_url: str = ""
    source_url: str = ""
    explicit: bool = False

    @property
    def uid(self) -> str:
        return f"{self.provider}:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Track":
        return cls(**raw)


@dataclass
class SearchPage:
    """One page of results plus everything needed to ask for the next one."""

    tracks: list[Track] = field(default_factory=list)
    has_more: bool = False
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracks": [t.to_dict() for t in self.tracks],
            "has_more": self.has_more,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SearchPage":
        return cls(
            tracks=[Track.from_dict(t) for t in raw.get("tracks", [])],
            has_more=bool(raw.get("has_more")),
            provider=raw.get("provider", ""),
        )
