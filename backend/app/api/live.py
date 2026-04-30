"""Continuous live radio stream with server-side crossfade for gapless transitions."""
import asyncio
import logging
import shutil
import time
import tempfile
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import config
from ..services.audio_cache import AudioCache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live"])


def _app_state():
    from ..main import app_state
    return app_state


def _get_audio_cache() -> AudioCache:
    return _app_state()["audio_cache"]


def _get_radio_state() -> dict | None:
    redis_state = _app_state().get("redis")
    return redis_state.get_radio_state() if redis_state else None


async def _stream_track_with_crossfade(path, seek_seconds: float, video_id: str, next_path: str = None, crossfade_dur: float = 3.0):
    """Stream audio with optional crossfade to next track."""
    # Build ffmpeg command
    # If next_path provided, mix with crossfade
    
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{max(0, seek_seconds):.3f}",
        "-re", "-i", str(path),
    ]
    
    filter_str = None
    if next_path and os.path.exists(next_path):
        # Dual input: current + next with crossfade
        # acrossfade=d=3 means 3-second crossfade
        cmd.extend(["-ss", "0", "-re", "-i", str(next_path)])
        # acrossfade will mix last 3s of input0 with first 3s of input1
        filter_str = f"acrossfade=d={crossfade_dur}:c1=tri:c2=tri"
        logger.info(f"Crossfade enabled: {video_id} -> next with {crossfade_dur}s")
    
    cmd.extend([
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "160k",
        "-f", "mp3",
        "pipe:1",
    ])
    
    if filter_str:
        cmd.insert(-4, "-filter_complex")
        cmd.insert(-4, filter_str)
    
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for /api/live/stream")
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    try:
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            state = _get_radio_state()
            current_id = state.get("current_track_id") if state else None
            current_id = current_id or state.get("track_id") if state else None
            # Stop if track changed
            if current_id and current_id != video_id:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if proc.returncode not in (0, None):
            err = (await proc.stderr.read()).decode("utf-8", "ignore")
            if err:
                logger.warning("ffmpeg live stream error: %s", err[:500])


async def live_stream_generator():
    cache = _get_audio_cache()
    last_track_id = None
    
    while True:
        state = _get_radio_state()
        video_id = state.get("current_track_id") if state else None
        video_id = video_id or state.get("track_id") if state else None
        
        if not video_id:
            await asyncio.sleep(1)
            continue
        
        path = cache.path_for(video_id)
        if not path:
            try:
                await cache.prepare(video_id)
                path = cache.path_for(video_id)
            except Exception as exc:
                logger.warning("Live stream prepare failed for %s: %s", video_id, exc)
                await asyncio.sleep(2)
                continue
        
        if not path:
            await asyncio.sleep(1)
            continue
        
        now_ms = int(time.time() * 1000)
        started_at_ms = int(state.get("started_at_ms") or now_ms)
        elapsed_seconds = max(0, (now_ms - started_at_ms) / 1000)
        
        # Check queue for next track to preload/preview
        next_track_id = None
        next_path = None
        
        # Check Redis queue for next track to enable crossfade preload
        try:
            from ..main import app_state
            redis_state = app_state().get("redis")
            if redis_state:
                queue = redis_state.get_queue()
                if queue and len(queue) > 0:
                    next_track_id = queue[0].get("video_id") or queue[0].get("id")
                    if next_track_id:
                        next_path = cache.path_for(next_track_id)
                        if not next_path:
                            try:
                                await cache.prepare(next_track_id)
                                next_path = cache.path_for(next_track_id)
                            except Exception:
                                next_path = None
        except Exception:
            pass
        
        # Stream with crossfade if we have next track ready
        async for chunk in _stream_track_with_crossfade(path, elapsed_seconds, video_id, next_path):
            yield chunk
        
        await asyncio.sleep(0.15)
        last_track_id = video_id


@router.get("/stream")
async def stream_live_radio():
    try:
        return StreamingResponse(
            live_stream_generator(),
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "X-Accel-Buffering": "no",
            },
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "Live stream is not ready") from exc
