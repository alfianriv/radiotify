"""SQLite database layer for track metadata, play history, and cache."""
import sqlite3
import time
from typing import Optional, List, Dict, Any


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    video_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT,
                    album TEXT,
                    duration_seconds INTEGER,
                    thumbnail_url TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS play_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    played_at REAL NOT NULL,
                    source TEXT DEFAULT 'auto',
                    FOREIGN KEY (video_id) REFERENCES tracks(video_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    video_id TEXT,
                    performed_at REAL NOT NULL,
                    details TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS listener_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    count INTEGER NOT NULL,
                    recorded_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_listener_stats_recorded_at
                ON listener_stats(recorded_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            # Seed default config values
            conn.execute("""
                INSERT OR IGNORE INTO app_config (key, value, updated_at)
                VALUES ('maintenance_mode', 'false', ?)
            """, (time.time(),))
            conn.execute("""
                INSERT OR IGNORE INTO app_config (key, value, updated_at)
                VALUES ('maintenance_message', 'Service temporarily unavailable. We will be back soon.', ?)
            """, (time.time(),))
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_play_history_played_at
                ON play_history(played_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_play_history_video_id
                ON play_history(video_id)
            """)

    # ── Track CRUD ──────────────────────────────────────────

    def upsert_track(self, video_id: str, title: str, artist: str = None,
                     album: str = None, duration_seconds: int = None,
                     thumbnail_url: str = None) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tracks (video_id, title, artist, album, duration_seconds, thumbnail_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    title=excluded.title,
                    artist=excluded.artist,
                    album=excluded.album,
                    duration_seconds=excluded.duration_seconds,
                    thumbnail_url=excluded.thumbnail_url,
                    updated_at=excluded.updated_at
            """, (video_id, title, artist, album, duration_seconds, thumbnail_url, now, now))

    def get_track(self, video_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
            return dict(row) if row else None

    def get_tracks(self, video_ids: List[str]) -> List[Dict[str, Any]]:
        if not video_ids:
            return []
        placeholders = ','.join('?' * len(video_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM tracks WHERE video_id IN ({placeholders})", video_ids
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Play History ────────────────────────────────────────

    def add_play_history(self, video_id: str, source: str = 'auto') -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO play_history (video_id, played_at, source) VALUES (?, ?, ?)",
                (video_id, time.time(), source)
            )

    def get_recently_played(self, limit: int = 50) -> List[str]:
        """Return video IDs of recently played tracks (most recent first)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT video_id FROM play_history ORDER BY played_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [r['video_id'] for r in rows]

    def get_last_played(self) -> Optional[Dict[str, Any]]:
        """Get the last played track with metadata."""
        with self._conn() as conn:
            row = conn.execute("""
                SELECT t.* FROM play_history ph
                JOIN tracks t ON t.video_id = ph.video_id
                ORDER BY ph.played_at DESC LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    def get_top_tracks(self, days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
        """Most-played tracks within the last `days`, with play counts."""
        since = time.time() - days * 86400
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT t.video_id, t.title, t.artist, t.thumbnail_url,
                       COUNT(*) as play_count
                FROM play_history ph
                JOIN tracks t ON t.video_id = ph.video_id
                WHERE ph.played_at > ?
                GROUP BY ph.video_id
                ORDER BY play_count DESC, MAX(ph.played_at) DESC
                LIMIT ?
            """, (since, limit)).fetchall()
            return [dict(r) for r in rows]

    # ── Admin Actions Log ───────────────────────────────────

    def log_admin_action(self, action: str, video_id: str = None, details: str = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO admin_actions (action, video_id, performed_at, details) VALUES (?, ?, ?, ?)",
                (action, video_id, time.time(), details)
            )

    # ── History (for display) ───────────────────────────────

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT t.video_id, t.title, t.artist, t.thumbnail_url, ph.played_at, ph.source
                FROM play_history ph
                JOIN tracks t ON t.video_id = ph.video_id
                ORDER BY ph.played_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    # ── App Config ─────────────────────────────────────────

    def get_config(self, key: str, default: str = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key = ?", (key,)
            ).fetchone()
            return row['value'] if row else default

    def set_config(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, time.time())
            )

    def is_maintenance_mode(self) -> bool:
        return self.get_config('maintenance_mode', 'false') == 'true'

    def get_maintenance_message(self) -> str:
        return self.get_config('maintenance_message', 'Service temporarily unavailable.')

    # ── Listener Stats ─────────────────────────────────────

    def record_listener_count(self, count: int) -> None:
        """Record a listener count snapshot."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO listener_stats (count, recorded_at) VALUES (?, ?)",
                (count, time.time())
            )

    def get_listener_stats(self) -> dict:
        """Summary of listener counts: peak, average, current, total updates."""
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total_updates,
                    MAX(count) as peak,
                    ROUND(AVG(count), 1) as average,
                    (SELECT count FROM listener_stats ORDER BY recorded_at DESC LIMIT 1) as current
                FROM listener_stats
            """).fetchone()
            return dict(row) if row else {}

    # ── Observability Stats ────────────────────────────────

    def get_stats(self) -> dict:
        """Get observability stats."""
        with self._conn() as conn:
            total_plays = conn.execute("SELECT COUNT(*) FROM play_history").fetchone()[0]
            # Plays in last 24h
            day_ago = time.time() - 86400
            plays_24h = conn.execute("SELECT COUNT(*) FROM play_history WHERE played_at > ?", (day_ago,)).fetchone()[0]
            # Top tracks (last 7 days)
            week_ago = time.time() - 604800
            top_rows = conn.execute("""
                SELECT t.title, t.artist, COUNT(*) as cnt 
                FROM play_history ph JOIN tracks t ON t.video_id = ph.video_id
                WHERE ph.played_at > ? GROUP BY ph.video_id ORDER BY cnt DESC LIMIT 5
            """, (week_ago,)).fetchall()
            top_tracks = [dict(r) for r in top_rows]
            # Source breakdown (last 24h)
            source_rows = conn.execute("""
                SELECT source, COUNT(*) as cnt FROM play_history 
                WHERE played_at > ? GROUP BY source
            """, (day_ago,)).fetchall()
            sources = {r['source']: r['cnt'] for r in source_rows}
            # Uptime tracking (first play)
            first = conn.execute("SELECT MIN(played_at) FROM play_history").fetchone()[0]

            return {
                'total_plays': total_plays,
                'plays_24h': plays_24h,
                'top_tracks': top_tracks,
                'sources': sources,
                'first_play_at': first,
            }
