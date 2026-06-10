"""Radio state API endpoints."""
import time
import logging
import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..models import RadioStateResponse, VoteSkipResponse
from ..services.redis_state import RedisState
from ..services.db import Database

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radio", tags=["radio"])


def get_redis() -> RedisState:
    from ..main import app_state
    return app_state['redis']


def get_db() -> Database:
    from ..main import app_state
    return app_state['db']


def get_engine():
    from ..main import app_state
    return app_state['engine']


def get_audio_cache():
    from ..main import app_state
    return app_state.get('audio_cache')


def get_youtube():
    from ..main import app_state
    return app_state.get('youtube')


@router.get("/state", response_model=RadioStateResponse)
async def get_radio_state():
    """Get current radio state. Client uses this to sync."""
    redis: RedisState = get_redis()
    db: Database = get_db()
    state = redis.get_radio_state()
    maintenance = db.is_maintenance_mode()
    maintenance_message = db.get_maintenance_message() if maintenance else None

    if not state:
        return RadioStateResponse(
            track_id="",
            meta={"video_id": "", "title": "Loading...", "artist": ""},
            started_at_ms=0,
            transition_at_ms=0,
            server_time_ms=time.time() * 1000,
            maintenance=maintenance,
            maintenance_message=maintenance_message,
        )

    meta = state.get('current_track_meta', {})
    next_meta = state.get('next_track_meta')
    audio_cache = get_audio_cache()
    audio = audio_cache.status(state['current_track_id']) if audio_cache else {}
    next_audio = (
        audio_cache.status(state['next_track_id'])
        if audio_cache and state.get('next_track_id') else {}
    )

    return RadioStateResponse(
        track_id=state['current_track_id'],
        meta=meta,
        next_track_id=state.get('next_track_id'),
        next_track_meta=next_meta,
        started_at_ms=state['started_at_ms'],
        transition_at_ms=state['transition_at_ms'],
        server_time_ms=time.time() * 1000,
        audio_url=audio.get('audio_url'),
        audio_status=audio.get('status'),
        next_audio_url=next_audio.get('audio_url'),
        next_audio_status=next_audio.get('status'),
        maintenance=maintenance,
        maintenance_message=maintenance_message,
    )


@router.get("/lyrics/{video_id}")
async def get_lyrics(video_id: str):
    """Get synchronized lyrics for a track.
    
    Returns: {lyrics: [{time_ms: int, text: str}, ...]} or {lyrics: null}
    """
    redis: RedisState = get_redis()
    youtube = get_youtube()

    if not youtube:
        return JSONResponse(
            status_code=503,
            content={"error": "YouTube service not available", "lyrics": None}
        )

    # Check Redis cache first
    cache_key = f"lyrics:{video_id}"
    try:
        cached = redis.get(cache_key)
        if cached:
            return {"lyrics": json.loads(cached)}
    except Exception:
        pass

    # Fetch from YouTube Music
    try:
        lyrics = youtube.get_lyrics(video_id)
        if lyrics:
            # Cache for 1 hour
            redis.setex(cache_key, 3600, json.dumps(lyrics))
            return {"lyrics": lyrics}
        else:
            # Cache empty result for 5 minutes to avoid repeated failed lookups
            redis.setex(cache_key, 300, json.dumps(None))
            return {"lyrics": None}
    except Exception as e:
        logger.error(f"Lyrics fetch error for {video_id}: {e}")
        return {"lyrics": None}


@router.post("/vote-skip", response_model=VoteSkipResponse)
async def vote_skip(request: Request):
    """Vote to skip the current track. Auto-skips when threshold is met."""
    redis: RedisState = get_redis()
    engine = get_engine()
    from ..main import ws_connections, broadcast_event

    # Use client IP as voter ID
    client_id = request.client.host if request.client else 'unknown'

    votes = redis.add_skip_vote(client_id)
    listeners = max(len(ws_connections), 1)
    needed = max(2, listeners // 2)  # 50% of listeners, min 2

    if votes >= needed:
        redis.clear_skip_votes()
        await engine.admin_skip()
        return VoteSkipResponse(votes=votes, needed=needed, skipped=True)

    # Broadcast skip votes update
    await broadcast_event('SKIP_VOTES', {'votes': votes, 'needed': needed})

    return VoteSkipResponse(votes=votes, needed=needed, skipped=False)


@router.post("/playback-resumed")
async def playback_resumed():
    """Called by a client when it successfully starts playing after maintenance.
    Broadcasts PLAYBACK_RESUMED so all other devices hide their maintenance overlay.
    """
    from ..main import broadcast_event
    db = get_db()
    # Only meaningful if maintenance was recently disabled
    if not db.is_maintenance_mode():
        await broadcast_event('PLAYBACK_RESUMED', {})
    return {"ok": True}


@router.get("/stats")
async def get_stats():
    """Get observability stats."""
    redis = get_redis()
    db = get_db()
    stats = db.get_stats()
    stats['queue_length'] = redis.get_queue_length()
    stats['uptime_seconds'] = time.time() - stats['first_play_at'] if stats['first_play_at'] else 0
    from ..main import ws_connections
    stats['listeners'] = len(ws_connections)
    stats['listener_stats'] = db.get_listener_stats()
    return stats
