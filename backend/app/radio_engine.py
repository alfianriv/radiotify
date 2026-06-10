"""Radio Engine Worker — deterministic timeline, track selection, transitions."""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable

from .config import config
from .services.redis_state import RedisState
from .services.youtube import YouTubeMusicService
from .services.db import Database

logger = logging.getLogger(__name__)


class RadioEngine:
    """
    Deterministic radio engine.
    
    Single source of truth: Server clock.
    Radio state stored in Redis:
        {
            "current_track_id": "abc123",
            "current_track_meta": { title, artist, duration_seconds, thumbnail },
            "next_track_id": "def456",
            "next_track_meta": { ... },
            "started_at_ms": 1714300000000,
            "transition_at_ms": 1714300210000,
        }
    """

    def __init__(self, redis: RedisState, youtube: YouTubeMusicService, db: Database):
        self.redis = redis
        self.youtube = youtube
        self.db = db
        self._running = False
        self._broadcast_fn: Optional[Callable[[str, Any], Awaitable[None]]] = None
        self._tick_interval = 1.0  # seconds

    def set_broadcast_fn(self, fn: Callable[[str, Any], Awaitable[None]]):
        """Set WebSocket broadcast function."""
        self._broadcast_fn = fn

    async def broadcast(self, event: str, data: Any):
        if self._broadcast_fn:
            try:
                await self._broadcast_fn(event, data)
            except Exception as e:
                logger.error(f"Broadcast error: {e}")

    def _prepare_audio_background(self, video_id: Optional[str]):
        if not video_id:
            return
        try:
            from .main import app_state
            audio_cache = app_state.get('audio_cache')
            if audio_cache:
                audio_cache.prepare_background(video_id)
        except Exception:
            pass

    def _prefetch_hls(self, video_id: str):
        """Download audio + pre-encode HLS for a track in background.
        Used for look-ahead: encode next track before it's needed so
        skip is instant (no 503 waiting for encode)."""
        async def _run():
            try:
                from .main import app_state
                cache = app_state.get('audio_cache')
                worker = app_state.get('hls_worker')
                if not cache or not worker:
                    return
                # Skip if HLS already ready
                if worker.is_track_ready(video_id):
                    logger.info("HLS prefetch: %s already ready, skipping", video_id)
                    return
                # Download audio if not cached
                path = cache.path_for(video_id)
                if not path:
                    logger.info("HLS prefetch: downloading audio for %s", video_id)
                    await cache.prepare(video_id)
                    path = cache.path_for(video_id)
                if not path:
                    logger.warning("HLS prefetch: audio download failed for %s", video_id)
                    return
                # Encode HLS — but only if this track is still next (not already playing)
                state = None
                try:
                    redis = app_state.get('redis')
                    if redis:
                        state = redis.get_radio_state()
                except Exception:
                    pass
                current = state.get('current_track_id') if state else None
                if current == video_id:
                    # Already playing — _notify_hls_worker handles this
                    return
                logger.info("HLS prefetch: encoding HLS for next track %s", video_id)
                worker.prefetch_track(video_id, path)
            except Exception as exc:
                logger.warning("HLS prefetch error for %s: %s", video_id, exc)
        asyncio.create_task(_run())

    def _notify_hls_worker(self, video_id: str):
        """Notify HLS worker that a new track should be streamed.
        If audio is already cached, notify immediately.
        Otherwise wait for download to complete in background."""
        try:
            from .main import app_state
            worker = app_state.get('hls_worker')
            cache = app_state.get('audio_cache')
            if not worker or not cache:
                return
            path = cache.path_for(video_id)
            if path and path.exists():
                worker.notify_track_change(video_id, path)
                return
            # Audio not cached yet — wait for it then notify
            async def _wait_and_notify():
                try:
                    await cache.prepare(video_id)
                    p = cache.path_for(video_id)
                    if p and p.exists():
                        worker.notify_track_change(video_id, p)
                except Exception as exc:
                    logger.warning("HLS notify failed for %s: %s", video_id, exc)
            asyncio.create_task(_wait_and_notify())
        except Exception as exc:
            logger.warning("_notify_hls_worker error: %s", exc)

    def _is_valid_track(self, track: Optional[Dict[str, Any]]) -> bool:
        """Reject preview/short clips before they enter radio state/history."""
        if not track:
            return False
        title = (track.get('title') or '').lower()
        duration = track.get('duration_seconds') or 0
        return 'preview' not in title and duration >= 60

    # ── Main Loop ───────────────────────────────────────────

    async def start(self):
        """Start the radio engine loop."""
        logger.info("📻 Radio Engine starting...")
        self._running = True

        # Cold start check
        state = self.redis.get_radio_state()
        if not state:
            logger.info("Cold start — no state found, initializing...")
            await self._cold_start()
        else:
            logger.info(f"Resuming — current track: {state.get('current_track_id')}")
            # Check if current track already ended while we were down
            now_ms = time.time() * 1000
            transition_at = state.get('transition_at_ms', 0)
            if now_ms >= transition_at:
                logger.info("Track ended while offline — transitioning...")
                await self._transition()
            else:
                # Re-encode HLS for current track (old folder may have been cleaned up)
                current_id = state.get('current_track_id')
                if current_id:
                    logger.info("Resume: re-notifying HLS worker for current track %s", current_id)
                    self._notify_hls_worker(current_id)
                # Ensure next track is prepared
                await self._ensure_next_track()

        # Persist engine start time for uptime tracking
        self.redis.set_engine_start_time()

        # Main tick loop
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}")
            await asyncio.sleep(self._tick_interval)

    async def stop(self):
        self._running = False
        logger.info("📻 Radio Engine stopped")

    async def _tick(self):
        """Called every second."""
        # Pause engine during maintenance mode — check DB flag
        try:
            from .main import app_state
            db: Database = app_state.get('db')
            if db and db.is_maintenance_mode():
                return
        except Exception:
            pass

        # Auto-pause when no listeners — save resources
        try:
            from .main import ws_connections
            if len(ws_connections) == 0:
                state = self.redis.get_radio_state()
                if state and state.get('current_track_id') and not state.get('paused_for_no_listeners'):
                    now_ms = time.time() * 1000
                    started_at = state.get('started_at_ms', now_ms)
                    state['paused_elapsed_ms'] = now_ms - started_at
                    state['paused_at_ms'] = now_ms
                    state['paused_for_no_listeners'] = True
                    self.redis.set_radio_state(state)
                    logger.info("⏸ No listeners — pausing playback")
                return
        except Exception:
            pass

        # Resume if was paused for no listeners and now has listeners
        state = self.redis.get_radio_state()
        if state and state.get('paused_for_no_listeners'):
            try:
                from .main import ws_connections
                if len(ws_connections) > 0:
                    logger.info("▶ Listener connected — resuming playback")
                    await self.resume_after_no_listeners()
                    return
            except Exception:
                pass

        state = self.redis.get_radio_state()
        if not state:
            await self._cold_start()
            return

        now_ms = time.time() * 1000
        transition_at = state.get('transition_at_ms', 0)
        current_duration_ms = (transition_at - state.get('started_at_ms', 0))
        notify_before = config.NEXT_TRACK_NOTIFY_S * 1000

        current_meta = state.get('current_track_meta') or {}
        if not self._is_valid_track(current_meta):
            logger.info(f"Skipping invalid current track: {current_meta.get('title')}")
            await self._transition(skip_history=True)
            return

        # Check admin skip
        if self.redis.get_admin_skip():
            logger.info("Admin skip requested")
            await self._transition(skip_history=True)
            return

        # Check admin force play
        force_id = self.redis.get_admin_force_play()
        if force_id:
            logger.info(f"Admin force play: {force_id}")
            self.redis.clear_admin_force_play()
            await self._play_track(force_id, source='admin')
            return

        # Preload next track (20s before end)
        if not state.get('next_track_id') and transition_at - now_ms <= notify_before:
            await self._prepare_next_track()
            # Notify clients about next track
            state = self.redis.get_radio_state()
            if state and state.get('next_track_id'):
                await self.broadcast('NEXT_TRACK', {
                    'track_id': state['next_track_id'],
                    'meta': state.get('next_track_meta'),
                })

        # Track ended — transition
        if now_ms >= transition_at:
            await self._transition()

    # ── Track Selection ─────────────────────────────────────

    async def _select_next_track(self, pop_queue: bool = False) -> Optional[Dict[str, Any]]:
        """
        Select next track by priority:
        1. User queue
        2. YouTube Music Up Next
        3. Random popular track
        """
        # Priority 1: User queue
        queue = self.redis.get_queue()
        if queue:
            if pop_queue:
                item = self.redis.pop_queue_front()
            else:
                item = queue[0]  # Peek, don't pop
            if item:
                video_id = item['video_id']
                if pop_queue:
                    await self.broadcast('QUEUE_UPDATED', {'queue': self.redis.get_queue()})
                track = await self._resolve_track(video_id)
                if self._is_valid_track(track):
                    return track
                logger.info(f"Skipping invalid queued track: {track.get('title') if track else video_id}")

        # Priority 2: YouTube Music Up Next
        state = self.redis.get_radio_state()
        if state and state.get('current_track_id'):
            up_next = self.youtube.get_up_next(state['current_track_id'], limit=15)
            if up_next:
                # Filter recently played
                recent = self.redis.get_recent_history(30)
                candidates = [vid for vid in up_next if vid not in recent]
                import random
                random.shuffle(candidates)  # avoid always picking same track
                for candidate in candidates:
                    track = await self._resolve_track(candidate)
                    if self._is_valid_track(track):
                        return track
                    logger.info(f"Skipping invalid up-next track: {track.get('title') if track else candidate}")

        # Priority 3: Random track
        recent = self.redis.get_recent_history(50)
        track = self.youtube.get_random_track(exclude=recent)
        return track

    async def _resolve_track(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Resolve video_id to full track metadata."""
        return self.youtube.get_track_info(video_id)

    # ── Transitions ─────────────────────────────────────────

    async def _transition(self, skip_history: bool = False):
        """Transition to next track."""
        state = self.redis.get_radio_state()

        # Use preloaded next track if available
        next_track = None
        if state and state.get('next_track_id'):
            next_meta = state.get('next_track_meta', {})
            next_track = {
                'video_id': state['next_track_id'],
                'title': next_meta.get('title'),
                'artist': next_meta.get('artist'),
                'duration_seconds': next_meta.get('duration_seconds'),
                'thumbnail': next_meta.get('thumbnail'),
            }

        if next_track and not self._is_valid_track(next_track):
            logger.info(f"Skipping invalid preloaded track: {next_track.get('title')}")
            next_track = None

        if not next_track:
            next_track = await self._select_next_track(pop_queue=True)

        if not next_track or not self._is_valid_track(next_track):
            logger.error("No track available! Retrying in 5s...")
            await asyncio.sleep(5)
            # Retry — cold start as last resort
            await self._cold_start()
            return

        # Pop queue if using preloaded next track from queue
        if next_track and self.redis.get_queue():
            top = self.redis.get_queue()[0]
            if top and top.get('video_id') == next_track.get('video_id'):
                self.redis.pop_queue_front()
                await self.broadcast('QUEUE_UPDATED', {'queue': self.redis.get_queue()})

        # Add current track to history BEFORE transitioning (unless skipped)
        if state and state.get('current_track_id') and not skip_history:
            self.redis.add_to_history(state['current_track_id'])
            self.db.add_play_history(state['current_track_id'], source='auto')

        await self._play_track(next_track['video_id'], meta=next_track, source='auto', add_history=False)

    async def _play_track(self, video_id: str, meta: Dict[str, Any] = None,
                          source: str = 'auto', add_history: bool = True):
        """Start playing a track."""
        if not meta:
            meta = await self._resolve_track(video_id)
        if not meta:
            logger.error(f"Cannot resolve track {video_id}")
            return
        if not self._is_valid_track(meta):
            logger.info(f"Skipping invalid track before play: {meta.get('title')}")
            replacement = await self._select_next_track(pop_queue=True)
            if replacement and replacement.get('video_id') != video_id:
                await self._play_track(replacement['video_id'], meta=replacement, source=source, add_history=add_history)
            return

        now_ms = time.time() * 1000
        duration_ms = (meta.get('duration_seconds') or 180) * 1000
        play_window_ms = max(1000, duration_ms - config.CROSSFADE_MS)

        state = {
            'current_track_id': video_id,
            'current_track_meta': {
                'video_id': video_id,
                'title': meta.get('title', 'Unknown'),
                'artist': meta.get('artist', 'Unknown'),
                'duration_seconds': meta.get('duration_seconds'),
                'thumbnail': meta.get('thumbnail') or meta.get('thumbnail_url'),
            },
            'next_track_id': None,
            'next_track_meta': None,
            'started_at_ms': now_ms,
            'transition_at_ms': now_ms + play_window_ms,
        }

        self.redis.set_radio_state(state)
        self._prepare_audio_background(video_id)
        self._notify_hls_worker(video_id)
        if add_history:
            self.redis.add_to_history(video_id)
            self.db.add_play_history(video_id, source=source)

        if source == 'admin':
            self.db.log_admin_action('force_play', video_id)

        logger.info(f"▶ Now playing: {meta.get('title')} — {meta.get('artist')}")

        # Broadcast track change
        await self.broadcast('TRACK_CHANGED', {
            'track_id': video_id,
            'meta': state['current_track_meta'],
            'started_at_ms': now_ms,
            'transition_at_ms': now_ms + play_window_ms,
            'server_time_ms': now_ms,
        })

        # Clear skip votes on track change
        self.redis.clear_skip_votes()

    async def _prepare_next_track(self):
        """Preload next track metadata."""
        state = self.redis.get_radio_state()
        if not state or state.get('next_track_id'):
            return

        next_track = await self._select_next_track()
        if not next_track:
            return

        # Filter: avoid same artist consecutively
        current_meta = state.get('current_track_meta', {})
        if (current_meta.get('artist') and next_track.get('artist') and
                current_meta['artist'].lower() == next_track['artist'].lower()):
            # Try again
            alt = await self._select_next_track()
            if alt and alt.get('video_id') != next_track.get('video_id'):
                next_track = alt

        state['next_track_id'] = next_track['video_id']
        state['next_track_meta'] = {
            'video_id': next_track['video_id'],
            'title': next_track.get('title', 'Unknown'),
            'artist': next_track.get('artist', 'Unknown'),
            'duration_seconds': next_track.get('duration_seconds'),
            'thumbnail': next_track.get('thumbnail'),
        }
        self.redis.set_radio_state(state)
        self._prepare_audio_background(next_track['video_id'])
        self._prefetch_hls(next_track['video_id'])
        logger.info(f"⏭ Next prepared: {next_track.get('title')} — {next_track.get('artist')}")

    async def _ensure_next_track(self):
        """Ensure next track is prepared (called on resume)."""
        state = self.redis.get_radio_state()
        if state and not state.get('next_track_id'):
            await self._prepare_next_track()

    # ── Cold Start ──────────────────────────────────────────

    async def _cold_start(self):
        """Initialize radio with a random track."""
        logger.info("📻 Cold start — fetching random track...")
        track = self.youtube.get_random_track()
        if track:
            await self._play_track(track['video_id'], meta=track, source='auto')
        else:
            logger.error("Cold start failed — no tracks available!")

    # ── Public API ──────────────────────────────────────────

    async def resume_after_maintenance(self):
        """Called when maintenance mode is disabled.
        Restores playback from the exact position it was paused at.
        """
        logger.info("📻 Resuming after maintenance...")
        from .main import broadcast_event
        state = self.redis.get_radio_state()
        if not state:
            await self._cold_start()
            return

        now_ms = time.time() * 1000
        paused_elapsed_ms = state.get('paused_elapsed_ms')
        paused_at_ms = state.get('paused_at_ms')

        if paused_elapsed_ms is not None:
            # Rewind started_at_ms so elapsed = paused_elapsed_ms from now
            state['started_at_ms'] = now_ms - paused_elapsed_ms
            # Extend transition_at_ms by the maintenance duration
            if paused_at_ms:
                maintenance_duration_ms = now_ms - paused_at_ms
                state['transition_at_ms'] = state['transition_at_ms'] + maintenance_duration_ms
            # Clear pause markers
            state.pop('paused_elapsed_ms', None)
            state.pop('paused_at_ms', None)
            self.redis.set_radio_state(state)
            logger.info("Resume: restored elapsed=%.1fs", paused_elapsed_ms / 1000)

        transition_at = state.get('transition_at_ms', 0)
        if now_ms >= transition_at:
            logger.info("Track expired during maintenance — transitioning...")
            await self._transition()
        else:
            current_id = state.get('current_track_id')
            if current_id:
                self._notify_hls_worker(current_id)
            await broadcast_event('TRACK_CHANGED', {
                'track_id': state['current_track_id'],
                'meta': state.get('current_track_meta', {}),
                'started_at_ms': state['started_at_ms'],
                'transition_at_ms': state['transition_at_ms'],
                'server_time_ms': now_ms,
            })

    async def resume_after_no_listeners(self):
        """Called when first listener connects after all listeners left.
        Restores playback from the exact position it was paused at.
        """
        logger.info("▶ Resuming after no listeners...")
        from .main import broadcast_event
        state = self.redis.get_radio_state()
        if not state:
            await self._cold_start()
            return

        now_ms = time.time() * 1000
        paused_elapsed_ms = state.get('paused_elapsed_ms')
        paused_at_ms = state.get('paused_at_ms')

        if paused_elapsed_ms is not None:
            state['started_at_ms'] = now_ms - paused_elapsed_ms
            if paused_at_ms:
                pause_duration_ms = now_ms - paused_at_ms
                state['transition_at_ms'] = state['transition_at_ms'] + pause_duration_ms
            state.pop('paused_elapsed_ms', None)
            state.pop('paused_at_ms', None)
            state.pop('paused_for_no_listeners', None)
            self.redis.set_radio_state(state)
            logger.info("Resume: restored elapsed=%.1fs", paused_elapsed_ms / 1000)

        transition_at = state.get('transition_at_ms', 0)
        if now_ms >= transition_at:
            logger.info("Track expired while no listeners — transitioning...")
            await self._transition()
        else:
            current_id = state.get('current_track_id')
            if current_id:
                self._notify_hls_worker(current_id)
            await broadcast_event('TRACK_CHANGED', {
                'track_id': state['current_track_id'],
                'meta': state.get('current_track_meta', {}),
                'started_at_ms': state['started_at_ms'],
                'transition_at_ms': state['transition_at_ms'],
                'server_time_ms': now_ms,
            })

    async def admin_skip(self):
        """Skip current track immediately."""
        logger.info("Admin skip — transitioning immediately")
        self.redis.clear_admin_skip()  # clear flag so _tick() doesn't double-transition
        await self._transition(skip_history=True)

    async def admin_force_play(self, video_id: str):
        """Force play a specific track immediately."""
        logger.info(f"Admin force play — playing {video_id} immediately")
        self.redis.clear_admin_force_play()
        await self._play_track(video_id, source='admin')

    async def admin_clear_queue(self):
        """Clear the queue."""
        self.redis.clear_queue()
        await self.broadcast('QUEUE_UPDATED', {'queue': []})

    async def admin_lock_queue(self, locked: bool):
        """Lock/unlock the queue."""
        self.redis.set_queue_locked(locked)
        await self.broadcast('QUEUE_UPDATED', {'locked': locked})

    async def admin_remove_queue_item(self, index: int) -> bool:
        """Remove item from queue by index."""
        removed = self.redis.remove_from_queue(index)
        if removed:
            await self.broadcast('QUEUE_UPDATED', {'queue': self.redis.get_queue()})
            return True
        return False
