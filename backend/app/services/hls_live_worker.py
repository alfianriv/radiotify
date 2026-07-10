"""
HLS Pre-build Worker — encode all segments upfront per track (no -re).

Architecture:
  RadioEngine._play_track()
      → HlsLiveWorker.notify_track_change(track_id, audio_path)   ← HIGH priority, interrupts current encode
      → HlsLiveWorker.prefetch_track(track_id, audio_path)        ← LOW priority, queued after current encode

  HlsLiveWorker (background asyncio task)
      → ffmpeg (no -re, fast encode) per track
      → hls_streams/<track_id>/playlist.m3u8
      → hls_streams/<track_id>/seg_NNNNN.ts

  Client joins mid-track:
      → fetches /api/live/hls/<track_id>/playlist.m3u8
      → HLS.js buffers from current position forward
      → No lag — all segments already on disk

  On track change (skip):
      → HLS already pre-encoded (prefetch_track was called when track was queued)
      → New track encoded to new subfolder
      → Old folder cleaned up after 60s (clients may still be buffering)
"""
import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HLS_ROOT = Path(__file__).parent.parent.parent / "hls_streams"
SEGMENT_DUR = 4        # seconds per .ts segment
CLEANUP_DELAY = 60.0   # seconds to keep old track folder after track change
ENCODE_TIMEOUT = 120.0 # max seconds to wait for ffmpeg to finish encoding


class HlsLiveWorker:
    """
    Pre-builds HLS segments for each track as fast as possible (no -re).
    Clients buffer from current position forward — no lag on join.

    Two entry points:
      notify_track_change(track_id, path) — HIGH priority, interrupts current encode.
          Use when a track starts playing NOW.
      prefetch_track(track_id, path)      — LOW priority, queued after current encode.
          Use for look-ahead (next track in queue) so skip is instant.

    Usage:
        worker = HlsLiveWorker()
        asyncio.create_task(worker.start(audio_cache))
        ...
        worker.notify_track_change(track_id, audio_path)   # now playing
        worker.prefetch_track(next_track_id, next_path)    # look-ahead
    """

    def __init__(self):
        self._running = False
        self._audio_cache = None

        # HIGH priority: currently playing track (interrupts encode)
        self._pending_id: Optional[str] = None
        self._pending_path: Optional[Path] = None

        # LOW priority: prefetch queue (encoded after current finishes)
        self._prefetch_queue: list[tuple[str, Path]] = []

        self._track_event: Optional[asyncio.Event] = None

        # ffmpeg process handle
        self._proc: Optional[asyncio.subprocess.Process] = None

        # Track currently being encoded
        self._current_track_id: Optional[str] = None

        HLS_ROOT.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────────

    def notify_track_change(self, track_id: str, audio_path: Path):
        """
        HIGH PRIORITY — signal that this track is NOW playing.
        No-op during maintenance mode.
        """
        try:
            from ..main import app_state
            db = app_state.get('db')
            if db and db.is_maintenance_mode():
                logger.info("HLS notify skipped (maintenance): %s", track_id)
                return
        except Exception:
            pass
        # Remove from prefetch queue if it was there
        self._prefetch_queue = [(tid, p) for tid, p in self._prefetch_queue if tid != track_id]
        self._pending_id = track_id
        self._pending_path = audio_path
        if self._track_event is not None:
            self._track_event.set()
        logger.info("HLS worker notified (HIGH): track=%s path=%s", track_id, audio_path.name)

    def prefetch_track(self, track_id: str, audio_path: Path):
        """
        LOW PRIORITY — queue this track for background pre-encoding.
        No-op during maintenance mode.
        """
        try:
            from ..main import app_state
            db = app_state.get('db')
            if db and db.is_maintenance_mode():
                logger.info("HLS prefetch skipped (maintenance): %s", track_id)
                return
        except Exception:
            pass
        # Skip if already encoded
        if self.is_track_ready(track_id):
            logger.info("HLS prefetch: %s already ready, skipping", track_id)
            return
        # Skip if already in queue
        if any(tid == track_id for tid, _ in self._prefetch_queue):
            logger.info("HLS prefetch: %s already queued, skipping", track_id)
            return
        # Skip if it's the current high-priority track
        if self._pending_id == track_id or self._current_track_id == track_id:
            logger.info("HLS prefetch: %s already pending/encoding, skipping", track_id)
            return

        self._prefetch_queue.append((track_id, audio_path))
        if self._track_event is not None:
            self._track_event.set()
        logger.info("HLS worker queued (LOW): track=%s path=%s", track_id, audio_path.name)

    def get_track_dir(self, track_id: str) -> Path:
        """Return the HLS output directory for a given track."""
        return HLS_ROOT / track_id

    def is_track_ready(self, track_id: str) -> bool:
        """True if the track's HLS playlist exists on disk."""
        pl = self.get_track_dir(track_id) / "playlist.m3u8"
        return pl.exists()

    async def stop(self):
        """Gracefully stop the worker."""
        self._running = False
        if self._track_event:
            self._track_event.set()
        await self._kill_proc()

    async def start(self, audio_cache):
        """Main loop — runs forever, one ffmpeg encode per track."""
        self._running = True
        self._audio_cache = audio_cache
        self._track_event = asyncio.Event()

        # If there's already a pending track from before start(), encode it
        if self._pending_path and self._pending_path.exists():
            self._track_event.set()

        logger.info("HLS worker started")
        while self._running:
            try:
                await self._run_one_track()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("HLS worker error: %s", exc)
                await asyncio.sleep(1)

        await self._kill_proc()
        logger.info("HLS worker stopped")

    @property
    def ready(self) -> bool:
        """True once at least one track has been encoded."""
        return self._current_track_id is not None

    # ── Internal ────────────────────────────────────────────────────────────

    async def _wait_for_track(self) -> tuple[str, Path]:
        """Block until a track is available to encode (high or low priority)."""
        while self._running:
            # Pause during maintenance mode
            try:
                from ..main import app_state
                db = app_state.get('db')
                if db and db.is_maintenance_mode():
                    await asyncio.sleep(1)
                    continue
            except Exception:
                pass

            # HIGH priority first
            if self._pending_id and self._pending_path and self._pending_path.exists():
                track_id = self._pending_id
                path = self._pending_path
                self._pending_id = None
                self._pending_path = None
                self._track_event.clear()
                return track_id, path

            # LOW priority (prefetch queue)
            while self._prefetch_queue:
                track_id, path = self._prefetch_queue.pop(0)
                if path.exists() and not self.is_track_ready(track_id):
                    self._track_event.clear()
                    return track_id, path
                # Skip stale entries
                logger.info("HLS prefetch: skipping stale entry %s", track_id)

            # Poll audio cache for current track
            if self._audio_cache:
                result = await self._poll_audio_cache()
                if result:
                    return result

            # Wait for notification or poll again in 0.5s
            try:
                await asyncio.wait_for(self._track_event.wait(), timeout=0.5)
                self._track_event.clear()
            except asyncio.TimeoutError:
                pass

        raise asyncio.CancelledError("Worker stopped before track available")

    async def _poll_audio_cache(self) -> Optional[tuple[str, Path]]:
        """Try to get current track path from radio state + audio cache."""
        try:
            from ..main import app_state
            redis = app_state.get("redis")
            if not redis:
                return None
            state = redis.get_radio_state()
            if not state:
                return None
            vid = state.get("current_track_id") or state.get("track_id")
            if not vid:
                return None
            # Don't re-encode the same track
            if vid == self._current_track_id:
                return None
            # Skip if already encoded
            if self.is_track_ready(vid):
                self._current_track_id = vid
                return None
            path = self._audio_cache.path_for(vid)
            if path and path.exists():
                logger.info("HLS worker: found cached track %s", vid)
                return vid, path
        except Exception:
            pass
        return None

    async def _run_one_track(self):
        """
        Encode all HLS segments for the next track in queue.
        HIGH priority tracks interrupt current encode.
        LOW priority tracks wait for current encode to finish.
        Maintenance mode interrupts any ongoing encode immediately.
        """
        track_id, path = await self._wait_for_track()
        if not self._running:
            return

        # Skip if already encoded
        if self.is_track_ready(track_id):
            logger.info("HLS: track %s already encoded, skipping", track_id)
            self._current_track_id = track_id
            return

        old_track_id = self._current_track_id
        self._current_track_id = track_id
        logger.info("HLS: encoding track %s from %s", track_id, path.name)

        out_dir = self.get_track_dir(track_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = _build_ffmpeg_cmd(path, out_dir)
        logger.info("Starting ffmpeg: %s", " ".join(str(c) for c in cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("ffmpeg started (pid=%s) for %s", self._proc.pid, path.name)

        # Poll maintenance flag every second during encode
        async def _maintenance_waiter():
            while True:
                await asyncio.sleep(1)
                try:
                    from ..main import app_state
                    db = app_state.get('db')
                    if db and db.is_maintenance_mode():
                        return
                except Exception:
                    pass

        # Wait for: ffmpeg finishes, HIGH PRIORITY track signalled, or maintenance enabled
        ffmpeg_done = asyncio.create_task(self._proc.wait(), name="ffmpeg-wait")
        track_changed = asyncio.create_task(
            self._track_event.wait(), name="track-changed"
        )
        maintenance_on = asyncio.create_task(_maintenance_waiter(), name="maintenance-check")

        done, pending = await asyncio.wait(
            [ffmpeg_done, track_changed, maintenance_on],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=ENCODE_TIMEOUT,
        )
        for t in pending:
            t.cancel()

        if maintenance_on in done:
            # Maintenance enabled mid-encode — kill ffmpeg and clean up
            logger.info("HLS: maintenance enabled, interrupting encode of %s", track_id)
            await self._kill_proc()
            shutil.rmtree(out_dir, ignore_errors=True)
            self._current_track_id = old_track_id
            return

        if track_changed in done:
            # Only interrupt if there's a HIGH priority track pending
            if self._pending_id and self._pending_id != track_id:
                logger.info("HLS: HIGH priority track change, interrupting encode of %s", track_id)
                await self._kill_proc()
                # Clean up incomplete encode
                shutil.rmtree(out_dir, ignore_errors=True)
                self._current_track_id = old_track_id  # revert so old track stays "current"
                # Do NOT fall through to old-track cleanup: old_track_id is
                # the track still on air — deleting its folder kills playback
                return
            else:
                # Spurious event (prefetch queued) — let ffmpeg finish
                logger.info("HLS: spurious event during encode of %s, waiting for ffmpeg...", track_id)
                try:
                    ret = await asyncio.wait_for(self._proc.wait(), timeout=ENCODE_TIMEOUT)
                    if ret != 0:
                        shutil.rmtree(out_dir, ignore_errors=True)
                    else:
                        logger.info("HLS: track %s encoded successfully ✓", track_id)
                except asyncio.TimeoutError:
                    logger.warning("HLS: ffmpeg encode timed out for %s", track_id)
                    await self._kill_proc()
                    shutil.rmtree(out_dir, ignore_errors=True)
        elif ffmpeg_done in done:
            ret = self._proc.returncode
            if ret != 0:
                try:
                    stderr = (await asyncio.wait_for(
                        self._proc.stderr.read(), timeout=2
                    )).decode("utf-8", "ignore")[-500:]
                except Exception:
                    stderr = "(unreadable)"
                logger.warning("ffmpeg exited %d: %s", ret, stderr)
                # Clean up failed encode
                shutil.rmtree(out_dir, ignore_errors=True)
            else:
                logger.info("HLS: track %s encoded successfully ✓", track_id)
        else:
            # Timeout
            logger.warning("HLS: ffmpeg encode timed out for %s", track_id)
            await self._kill_proc()
            shutil.rmtree(out_dir, ignore_errors=True)

        # Schedule cleanup of old track folder
        if old_track_id and old_track_id != track_id:
            asyncio.create_task(self._cleanup_old_track(old_track_id))

    async def _cleanup_old_track(self, track_id: str):
        """Remove old track's HLS folder after a delay."""
        await asyncio.sleep(CLEANUP_DELAY)
        # Defense in depth: never delete the folder of whatever is current
        # by the time the delay elapses (e.g. after an interrupted encode)
        if track_id in (self._current_track_id, self._pending_id):
            logger.info("HLS: skip cleanup of %s — still current/pending", track_id)
            return
        old_dir = self.get_track_dir(track_id)
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
            logger.info("HLS: cleaned up old track folder %s", track_id)

    async def _kill_proc(self):
        """Kill current ffmpeg process if running."""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                pass
        self._proc = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _build_ffmpeg_cmd(audio_path: Path, out_dir: Path) -> list:
    """
    Build ffmpeg command for one track — fast encode, no -re.

    No -re: encode as fast as possible so segments are ready immediately.
    Clients seek to correct position using the full VOD playlist.
    hls_playlist_type vod: complete playlist with #EXT-X-ENDLIST.
    """
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        # Input audio file (no -re — encode as fast as possible)
        "-i", str(audio_path),
        # Audio: transcode to AAC
        "-vn",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-ar", "44100",
        # HLS output — VOD (all segments at once)
        "-f", "hls",
        "-hls_time", str(SEGMENT_DUR),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
        str(out_dir / "playlist.m3u8"),
    ]
