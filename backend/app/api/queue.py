"""Queue API endpoints."""
import uuid
import logging
from fastapi import APIRouter, HTTPException, Request

from ..models import (
    QueueAddRequest, QueueResponse, QueueItem,
    SearchResponse, SearchResult,
)
from ..services.redis_state import RedisState
from ..services.youtube import YouTubeMusicService
from ..services.db import Database
from ..config import config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/queue", tags=["queue"])


def _get_redis() -> RedisState:
    from ..main import app_state
    return app_state['redis']


def _get_youtube() -> YouTubeMusicService:
    from ..main import app_state
    return app_state['youtube']


def _get_db() -> Database:
    from ..main import app_state
    return app_state['db']


@router.get("", response_model=QueueResponse)
async def get_queue():
    """Get current queue."""
    redis: RedisState = _get_redis()
    queue = redis.get_queue()
    locked = redis.is_queue_locked()
    return QueueResponse(queue=queue, locked=locked)


@router.post("", response_model=QueueItem)
async def add_to_queue(req: QueueAddRequest, request: Request):
    """Add a song to the queue. Admin bypasses rate limit."""
    redis: RedisState = _get_redis()
    youtube: YouTubeMusicService = _get_youtube()

    # Check queue lock
    if redis.is_queue_locked():
        raise HTTPException(400, "Queue is locked")

    # Check if admin (bypass rate limit)
    is_admin = False
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        from .admin import verify_token
        is_admin = verify_token(auth[7:])

    # Rate limit per user (using IP as user_id for now)
    # TODO: implement proper user identification
    user_id = "default"
    if not is_admin and not redis.check_user_queue_limit(user_id):
        raise HTTPException(429, "Wait 3 minutes before adding another song")

    # Resolve track metadata if not provided
    title = req.title
    artist = req.artist
    thumbnail = None
    if not title:
        track = youtube.get_track_info(req.video_id)
        if track:
            title = track.get('title')
            artist = track.get('artist')
            thumbnail = track.get('thumbnail') or track.get('thumbnail_url')
        else:
            title = "Unknown"
            artist = "Unknown"

    item = QueueItem(
        video_id=req.video_id,
        title=title,
        artist=artist,
        thumbnail=thumbnail or f"https://img.youtube.com/vi/{req.video_id}/maxresdefault.jpg",
        added_by=user_id,
        source='user',
    )

    added = redis.add_to_queue(item.model_dump())
    if not added:
        raise HTTPException(409, "Song already in queue")

    redis.increment_user_queue_count(user_id)

    # Broadcast queue update
    from ..main import broadcast_event
    await broadcast_event('QUEUE_UPDATED', {'queue': redis.get_queue()})

    return item


@router.get("/search", response_model=SearchResponse)
async def search_tracks(q: str, limit: int = 5):
    """Search YouTube Music for songs."""
    youtube: YouTubeMusicService = _get_youtube()

    if len(q) < 3:
        raise HTTPException(400, "Query must be at least 3 characters")

    results = youtube.search(q, limit=limit)
    return SearchResponse(results=[
        SearchResult(**r) for r in results
    ])


@router.delete("/{index}")
async def remove_from_queue(index: int):
    """Remove item from queue."""
    redis: RedisState = _get_redis()
    removed = redis.remove_from_queue(index)
    if not removed:
        raise HTTPException(404, "Queue item not found")

    from ..main import broadcast_event
    await broadcast_event('QUEUE_UPDATED', {'queue': redis.get_queue()})

    return {"ok": True, "removed": removed}
