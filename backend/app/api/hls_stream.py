"""
HLS Pre-built Stream endpoints.

Each track is pre-encoded to its own folder:
  hls_streams/<track_id>/playlist.m3u8
  hls_streams/<track_id>/seg_NNNNN.ts

Client fetches /api/live/hls/<track_id>/playlist.m3u8 and buffers
from the current position forward — no lag, no live-stream stutter.

Endpoints:
  GET /api/live/hls/<track_id>/playlist.m3u8  — VOD playlist for track
  GET /api/live/hls/<track_id>/<seg>.ts       — individual segment
  GET /api/live/hls/master.m3u8              — current track playlist (compat)
"""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response

from ..services.hls_live_worker import HLS_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["hls-live"])


def _get_current_track_id() -> str | None:
    """Get current track ID from radio state."""
    try:
        from ..main import app_state
        redis = app_state.get("redis")
        if not redis:
            return None
        state = redis.get_radio_state()
        return state.get("current_track_id") or state.get("track_id") if state else None
    except Exception:
        return None


def _get_hls_worker():
    try:
        from ..main import app_state
        return app_state.get("hls_worker")
    except Exception:
        return None


def _is_maintenance() -> bool:
    """Check if maintenance mode is active."""
    try:
        from ..main import app_state
        db = app_state.get('db')
        return db.is_maintenance_mode() if db else False
    except Exception:
        return False


@router.get("/hls/master.m3u8")
async def hls_master():
    if _is_maintenance():
        raise HTTPException(503, detail="Service under maintenance")
    track_id = _get_current_track_id()
    if not track_id:
        raise HTTPException(503, detail="Radio not started yet")
    return RedirectResponse(
        url=f"/api/live/hls/{track_id}/playlist.m3u8",
        status_code=302,
    )


@router.get("/hls/{track_id}/playlist.m3u8")
async def hls_track_playlist(track_id: str):
    if _is_maintenance():
        raise HTTPException(503, detail="Service under maintenance")

    pl = HLS_ROOT / track_id / "playlist.m3u8"

    if not pl.exists():
        # Track may still be encoding — wait up to 10s
        worker = _get_hls_worker()
        for _ in range(20):
            await asyncio.sleep(0.5)
            if pl.exists():
                break
        else:
            # Track is not being encoded — redirect to current track if different
            current_id = _get_current_track_id()
            if current_id and current_id != track_id:
                logger.info(
                    "HLS: track %s not ready, redirecting to current track %s",
                    track_id, current_id,
                )
                return RedirectResponse(
                    url=f"/api/live/hls/{current_id}/playlist.m3u8",
                    status_code=302,
                )
            raise HTTPException(503, detail="Track HLS not ready yet, retry in a moment")

    return FileResponse(
        pl,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/hls/{track_id}/{segment}")
async def hls_track_segment(track_id: str, segment: str):
    if _is_maintenance():
        raise HTTPException(503, detail="Service under maintenance")
    if not segment.endswith(".ts"):
        raise HTTPException(404, detail="Not found")

    # Sanitize both path components
    if not all(c.isalnum() or c in "-_" for c in track_id):
        raise HTTPException(400, detail="Invalid track ID")

    seg_path = (HLS_ROOT / track_id / segment).resolve()
    # Prevent path traversal
    if not str(seg_path).startswith(str(HLS_ROOT.resolve())):
        raise HTTPException(403, detail="Forbidden")

    if not seg_path.exists():
        # Brief retry — segment may be mid-write by ffmpeg
        for _ in range(5):
            await asyncio.sleep(0.15)
            if seg_path.exists():
                break
        else:
            raise HTTPException(404, detail="Segment not found")

    return FileResponse(
        seg_path,
        media_type="video/mp2t",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "Access-Control-Allow-Origin": "*",
        },
    )
