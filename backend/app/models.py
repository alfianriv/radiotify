"""Pydantic models for API request/response."""
from typing import Optional, List, Any
from pydantic import BaseModel


# ── Radio State ─────────────────────────────────────────────

class TrackMeta(BaseModel):
    video_id: str
    title: str
    artist: Optional[str] = None
    duration_seconds: Optional[int] = None
    thumbnail: Optional[str] = None
    requested_by: Optional[str] = None
    dedication: Optional[str] = None


class RadioStateResponse(BaseModel):
    track_id: str
    meta: TrackMeta
    next_track_id: Optional[str] = None
    next_track_meta: Optional[TrackMeta] = None
    started_at_ms: float
    transition_at_ms: float
    server_time_ms: float
    audio_url: Optional[str] = None
    audio_status: Optional[str] = None
    next_audio_url: Optional[str] = None
    next_audio_status: Optional[str] = None
    maintenance: bool = False
    maintenance_message: Optional[str] = None
    mood: Optional[str] = None
    mood_emoji: Optional[str] = None


# ── Queue ───────────────────────────────────────────────────

class QueueItem(BaseModel):
    video_id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    thumbnail: Optional[str] = None
    added_by: Optional[str] = None
    message: Optional[str] = None
    source: str = 'user'


class QueueAddRequest(BaseModel):
    video_id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    nickname: Optional[str] = None
    message: Optional[str] = None


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


class QueueResponse(BaseModel):
    queue: List[QueueItem]
    locked: bool = False


# ── Admin ───────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    password: str


class AdminLoginResponse(BaseModel):
    token: str


class AdminForcePlayRequest(BaseModel):
    video_id: str


class AdminRemoveQueueRequest(BaseModel):
    index: int


# ── Search ──────────────────────────────────────────────────

class SearchResult(BaseModel):
    video_id: str
    title: str
    artist: Optional[str] = None
    duration_seconds: Optional[int] = None
    thumbnail: Optional[str] = None


class SearchResponse(BaseModel):
    results: List[SearchResult]


# ── History ─────────────────────────────────────────────────

class HistoryItem(BaseModel):
    video_id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    thumbnail: Optional[str] = None
    thumbnail_url: Optional[str] = None  # alias from DB
    played_at: float
    source: Optional[str] = None

    @property
    def resolved_thumbnail(self):
        return self.thumbnail or self.thumbnail_url

    model_config = {'populate_by_name': True}

    def model_post_init(self, __context):
        if not self.thumbnail and self.thumbnail_url:
            self.thumbnail = self.thumbnail_url


class HistoryResponse(BaseModel):
    history: List[HistoryItem]


class TopTrackItem(BaseModel):
    video_id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    thumbnail_url: Optional[str] = None
    play_count: int


class TopTracksResponse(BaseModel):
    top: List[TopTrackItem]
    days: int = 7


# ── Vote Skip ───────────────────────────────────────────────

class VoteSkipResponse(BaseModel):
    votes: int
    needed: int
    skipped: bool


# ── Stats ───────────────────────────────────────────────────

class StatsResponse(BaseModel):
    total_plays: int
    plays_24h: int
    top_tracks: list
    sources: dict
    first_play_at: Optional[float] = None
    uptime_seconds: Optional[float] = None
    queue_length: int = 0
    listeners: int = 0
