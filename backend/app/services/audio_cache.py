"""Audio cache and streaming helpers for optional native audio playback."""
import asyncio
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

import yt_dlp

from ..config import config


def _get_cookies_file() -> Optional[str]:
    """Return a writable copy of the cookies file, or None if not available."""
    src = config.COOKIES_FILE
    if not os.path.isfile(src):
        return None
    dst = '/tmp/yt_cookies.txt'
    if not os.path.isfile(dst):
        shutil.copy2(src, dst)
    return dst


class AudioCache:
    def __init__(self, cache_dir: Optional[str] = None):
        root = cache_dir or config.AUDIO_CACHE_DIR
        self.cache_dir = Path(root)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, video_id: str) -> Optional[Path]:
        for ext in ("m4a", "webm", "mp3", "opus"):
            path = self.cache_dir / f"{video_id}.{ext}"
            if path.exists() and path.stat().st_size > 0:
                return path
        return None

    def status(self, video_id: str) -> dict:
        path = self.path_for(video_id)
        if not path:
            return {"video_id": video_id, "status": "missing", "audio_url": None}
        return {
            "video_id": video_id,
            "status": "ready",
            "audio_url": f"/api/audio/{video_id}",
            "size": path.stat().st_size,
            "content_type": mimetypes.guess_type(path.name)[0] or "audio/mp4",
        }

    async def prepare(self, video_id: str) -> dict:
        if self.path_for(video_id):
            return self.status(video_id)
        lock = self._locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            if self.path_for(video_id):
                return self.status(video_id)
            await asyncio.to_thread(self._download, video_id)
            return self.status(video_id)

    def prepare_background(self, video_id: str):
        async def runner():
            try:
                await self.prepare(video_id)
            except Exception:
                # Preparation is best-effort; YouTube playback remains the fallback.
                pass
        asyncio.create_task(runner())

    def _download(self, video_id: str):
        output = str(self.cache_dir / f"{video_id}.%(ext)s")
        cookies_file = _get_cookies_file()
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            **(({"cookiefile": cookies_file}) if cookies_file else {}),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

    def open_range(self, video_id: str, range_header: Optional[str]) -> Tuple[Path, int, int, int, int]:
        path = self.path_for(video_id)
        if not path:
            raise FileNotFoundError(video_id)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            raw = range_header.replace("bytes=", "", 1).split(",", 1)[0]
            left, _, right = raw.partition("-")
            if left:
                start = max(0, int(left))
            if right:
                end = min(size - 1, int(right))
            status = 206
        return path, start, end, size, status

    @staticmethod
    def iter_file_range(path: Path, start: int, end: int, chunk_size: int = 1024 * 512):
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
