"""Tests for the SQLite database layer."""
import os
import tempfile

from app.services.db import Database


def make_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return Database(path)


def test_play_history_not_trimmed():
    db = make_db()
    for i in range(25):
        db.upsert_track(f'vid{i}', f'Title {i}', 'Artist')
        db.add_play_history(f'vid{i}')
    assert len(db.get_recently_played(limit=100)) == 25


def test_get_top_tracks():
    db = make_db()
    db.upsert_track('a', 'Song A', 'X')
    db.upsert_track('b', 'Song B', 'Y')
    for _ in range(3):
        db.add_play_history('a')
    db.add_play_history('b')
    top = db.get_top_tracks(days=7, limit=5)
    assert top[0]['video_id'] == 'a'
    assert top[0]['play_count'] == 3
    assert top[1]['video_id'] == 'b'
