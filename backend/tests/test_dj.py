"""Tests for the AI DJ service helpers."""
from app.services.dj import sanitize_script, build_context


def test_sanitize_strips_emoji_and_markdown():
    raw = '**Halo!** 🎉🔥 Barusan itu "Golden Hour" _keren_ banget! 🙌'
    out = sanitize_script(raw)
    assert '*' not in out and '"' not in out and '_' not in out
    assert '🎉' not in out and '🔥' not in out and '🙌' not in out
    assert 'Golden Hour' in out


def test_sanitize_collapses_whitespace():
    assert sanitize_script('  halo \n\n  dunia  ') == 'halo dunia'


def test_build_context_shape():
    ctx = build_context(
        {'title': 'Lagu A', 'artist': 'X'},
        {'title': 'Lagu B', 'artist': 'Y', 'requested_by': 'Alfian', 'dedication': 'semangat!'},
        mood='Night Vibes', listeners=7,
    )
    assert ctx['prev'] == 'Lagu A — X'
    assert ctx['next'] == 'Lagu B — Y'
    assert ctx['requested_by'] == 'Alfian'
    assert ctx['dedication'] == 'semangat!'
    assert ctx['mood'] == 'Night Vibes'
    assert ctx['listeners'] == 7


def test_build_context_handles_missing():
    ctx = build_context(None, None, mood='Late Night', listeners=0)
    assert ctx['prev'] == '? — ?'
    assert ctx['requested_by'] is None
