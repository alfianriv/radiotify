"""YouTube Music service using ytmusicapi + yt-dlp."""
import logging
import os
import random
from typing import Optional, List, Dict, Any

from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL

from .db import Database
from ..config import config

logger = logging.getLogger(__name__)


class YouTubeMusicService:
    def __init__(self, db: Database):
        self.db = db
        self.ytmusic = YTMusic()
        cookies_src = config.COOKIES_FILE
        # yt-dlp needs a writable cookies file; copy to /tmp if source is read-only
        cookies_file = None
        if os.path.isfile(cookies_src):
            import shutil
            cookies_file = '/tmp/yt_cookies.txt'
            if not os.path.isfile(cookies_file):
                shutil.copy2(cookies_src, cookies_file)
        self._ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'extractor_args': {'youtube': {'player_client': ['web_music']}},
            **(({'cookiefile': cookies_file}) if cookies_file else {}),
        }

    # ── Search ──────────────────────────────────────────────

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search YouTube Music for songs."""
        try:
            results = self.ytmusic.search(query, filter='songs', limit=limit)
            tracks = []
            for r in results:
                video_id = r.get('videoId')
                if not video_id:
                    continue
                artists = r.get('artists', [])
                artist_name = artists[0]['name'] if artists else 'Unknown'
                track = {
                    'video_id': video_id,
                    'title': r.get('title', 'Unknown'),
                    'artist': artist_name,
                    'duration_seconds': self._parse_duration(r.get('duration')),
                    'thumbnail': self._get_thumbnail(r),
                }
                tracks.append(track)
                # Cache metadata
                self.db.upsert_track(
                    video_id=track['video_id'],
                    title=track['title'],
                    artist=track['artist'],
                    duration_seconds=track['duration_seconds'],
                    thumbnail_url=track['thumbnail'],
                )
            return tracks
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    # ── Track Info ──────────────────────────────────────────

    def get_track_info(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Get track metadata. Check DB cache first, fallback to yt-dlp."""
        # Check cache
        cached = self.db.get_track(video_id)
        if cached and cached.get('duration_seconds'):
            return cached

        # Fetch via yt-dlp
        try:
            with YoutubeDL(self._ydl_opts) as ydl:
                info = ydl.extract_info(
                    f'https://youtube.com/watch?v={video_id}', download=False
                )
                if not info:
                    return None

                artist = (info.get('artist') or info.get('uploader') or
                          info.get('channel', 'Unknown'))
                # Clean " - Topic" suffix from YouTube Music channels
                if artist.endswith(' - Topic'):
                    artist = artist[:-8]

                track = {
                    'video_id': video_id,
                    'title': info.get('title', 'Unknown'),
                    'artist': artist,
                    'album': info.get('album'),
                    'duration_seconds': int(info.get('duration', 0)),
                    'thumbnail': self._best_thumbnail(info),
                }

                # Cache it
                self.db.upsert_track(
                    video_id=track['video_id'],
                    title=track['title'],
                    artist=track['artist'],
                    album=track.get('album'),
                    duration_seconds=track['duration_seconds'],
                    thumbnail_url=track['thumbnail'],
                )
                return track
        except Exception as e:
            logger.error(f"get_track_info error for {video_id}: {e}")
            return cached  # Return stale cache if available

    # ── Recommendations (Up Next) ───────────────────────────

    def get_up_next(self, video_id: str, limit: int = 10) -> List[str]:
        """Get related tracks from YouTube Music 'Up Next'."""
        try:
            watch_playlist = self.ytmusic.get_watch_playlist(videoId=video_id)
            tracks = watch_playlist.get('tracks', [])
            result = []
            for t in tracks:
                vid = t.get('videoId')
                if vid and vid != video_id:
                    # Cache metadata
                    artists = t.get('artists', [])
                    artist_name = artists[0]['name'] if artists else 'Unknown'
                    self.db.upsert_track(
                        video_id=vid,
                        title=t.get('title', 'Unknown'),
                        artist=artist_name,
                        duration_seconds=self._parse_duration(t.get('duration')),
                        thumbnail_url=self._get_thumbnail(t),
                    )
                    result.append(vid)
            return result[:limit]
        except Exception as e:
            logger.error(f"get_up_next error for {video_id}: {e}")
            return []

    # ── Random Track ────────────────────────────────────────

    def get_random_track(self, exclude: List[str] = None) -> Optional[Dict[str, Any]]:
        """Get a random popular track from charts."""
        exclude = exclude or []
        songs = []
        try:
            # Try trending songs first
            try:
                charts = self.ytmusic.get_charts()
                songs = charts.get('songs', {}).get('items', [])
            except (StopIteration, Exception) as charts_err:
                logger.warning(f"get_charts failed ({type(charts_err).__name__}), falling back to search")
                songs = []

            if not songs:
                # Fallback: search popular music
                results = self.ytmusic.search('popular music 2025', filter='songs', limit=20)
                songs = results

            candidates = [
                s for s in songs
                if s.get('videoId')
                and s['videoId'] not in exclude
                and '(preview)' not in s.get('title', '').lower()
                and 'preview' not in s.get('title', '').lower()
                and '( preview )' not in s.get('title', '').lower()
                and self._parse_duration(s.get('duration')) >= 90
            ]
        except Exception as e:
            logger.error(f"get_random_track error: {e}")
            candidates = []

        if not candidates:
            # Fallback: still require minimum 60s duration
            try:
                candidates = [
                    s for s in songs
                    if s.get('videoId')
                    and s['videoId'] not in exclude
                    and '(preview)' not in s.get('title', '').lower()
                    and '( preview )' not in s.get('title', '').lower()
                    and self._parse_duration(s.get('duration')) >= 60
                    and 'preview' not in s.get('title', '').lower()
                ]
            except Exception:
                candidates = []

        max_retries = 5
        for attempt in range(max_retries):
            if candidates:
                break
            # Coba fetch trending lagi
            try:
                try:
                    charts = self.ytmusic.get_charts()
                    songs = charts.get('songs', {}).get('items', [])
                except (StopIteration, Exception):
                    songs = self.ytmusic.search('popular music 2025', filter='songs', limit=20)
                candidates = [
                    s for s in songs
                    if s.get('videoId')
                    and s['videoId'] not in exclude
                    and '(preview)' not in s.get('title', '').lower()
                    and 'preview' not in s.get('title', '').lower()
                    and self._parse_duration(s.get('duration')) >= 90
                ]
            except Exception:
                candidates = []
        if not candidates:
            return None

        pick = random.choice(candidates)
        video_id = pick['videoId']
        artists = pick.get('artists', [])
        artist_name = artists[0]['name'] if artists else 'Unknown'

        track = {
            'video_id': video_id,
            'title': pick.get('title', 'Unknown'),
            'artist': artist_name,
            'duration_seconds': self._parse_duration(pick.get('duration')),
            'thumbnail': self._get_thumbnail(pick),
        }
        self.db.upsert_track(
            video_id=track['video_id'],
            title=track['title'],
            artist=track['artist'],
            duration_seconds=track['duration_seconds'],
            thumbnail_url=track['thumbnail'],
        )
        return track

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _parse_duration(duration_str: Optional[str]) -> Optional[int]:
        """Parse '3:45' or '1:23:45' to seconds."""
        if not duration_str:
            return None
        parts = duration_str.split(':')
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _get_thumbnail(item: dict) -> Optional[str]:
        """Get best thumbnail from ytmusicapi result."""
        thumbs = item.get('thumbnails', [])
        if thumbs:
            return thumbs[-1].get('url')
        return None

    @staticmethod
    def _best_thumbnail(info: dict) -> Optional[str]:
        """Get best thumbnail from yt-dlp result."""
        thumbs = info.get('thumbnails', [])
        if thumbs:
            return thumbs[-1].get('url')
        return None
