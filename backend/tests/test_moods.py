"""Tests for mood scheduling blocks."""
from app.services.moods import get_mood


def test_mood_blocks():
    assert get_mood(6)['name'] == 'Morning Fresh'
    assert get_mood(12)['name'] == 'Daytime Hits'
    assert get_mood(17)['name'] == 'Golden Hour'
    assert get_mood(22)['name'] == 'Night Vibes'
    assert get_mood(2)['name'] == 'Late Night'


def test_every_hour_has_mood():
    for h in range(24):
        mood = get_mood(h)
        assert mood['name'] and mood['queries']
