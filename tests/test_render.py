"""How a result looks: caption styles, source button, field limits."""

from __future__ import annotations

import pytest

from music_bot.models import PREVIEW_DURATION, Track
from music_bot.render import audio_result, audio_results, caption_for, result_id


def track(**over) -> Track:
    base = dict(
        id="1",
        title="Creep",
        artist="Radiohead",
        duration=238,
        preview_url="https://cdn/p.mp3",
        provider="deezer",
        album="Pablo Honey",
        source_url="https://www.deezer.com/track/1",
    )
    base.update(over)
    return Track(**base)


def test_caption_full_has_metadata():
    caption = caption_for(track(), "full")
    assert "<b>Creep</b>" in caption
    assert "Pablo Honey" in caption
    assert "3:58" in caption  # full length, not the preview length
    assert "превью 30 сек" in caption


def test_caption_short_is_just_title_and_artist():
    caption = caption_for(track(), "short")
    assert caption == "🎵 <b>Creep</b> — Radiohead"
    assert "Pablo Honey" not in caption


def test_caption_none_disables_it_entirely():
    assert caption_for(track(), "none") is None
    result = audio_result(track(), caption_style="none")
    assert result.caption is None
    # parse_mode must go away with the caption, not linger on an empty message.
    assert result.parse_mode is None
    # The audio player still names the track: that comes from title/performer.
    assert result.title == "Creep"
    assert result.performer == "Radiohead"


def test_source_button_can_be_switched_off():
    assert audio_result(track(), source_button=True).reply_markup is not None
    assert audio_result(track(), source_button=False).reply_markup is None


def test_button_label_follows_the_provider():
    deezer = audio_result(track(provider="deezer")).reply_markup
    itunes = audio_result(track(provider="itunes")).reply_markup
    assert "Deezer" in deezer.inline_keyboard[0][0].text
    assert "Apple Music" in itunes.inline_keyboard[0][0].text


def test_no_button_without_a_source_url():
    assert audio_result(track(source_url=""), source_button=True).reply_markup is None


def test_html_in_metadata_is_escaped():
    """Track titles come from a third party and land in an HTML caption."""
    caption = caption_for(track(title="A <b> & B", artist="X & Y"), "full")
    assert "&lt;b&gt;" in caption and "&amp;" in caption
    assert "<b>A" not in caption.replace("<b>A &lt;", "")


def test_explicit_is_marked_in_the_row_not_the_player():
    result = audio_result(track(explicit=True))
    assert result.title.endswith("🔞")


def test_duration_describes_the_preview_file():
    assert audio_result(track(duration=600)).audio_duration == PREVIEW_DURATION


def test_result_id_fits_telegrams_limit():
    assert len(result_id(track()).encode()) <= 64


def test_long_title_is_shortened():
    result = audio_result(track(title="x" * 200))
    assert len(result.title) <= 64


@pytest.mark.parametrize("style", ["full", "short", "none"])
def test_every_style_produces_valid_results(style):
    results = audio_results([track(), track(id="2")], caption_style=style)
    assert len(results) == 2
    assert all(r.type == "audio" for r in results)
