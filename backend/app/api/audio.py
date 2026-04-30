"""Optional native audio playback endpoints."""
import mimetypes

from fastapi import APIRouter, Header, HTTPException, Response
from fastapi.responses import StreamingResponse

from ..services.audio_cache import AudioCache

router = APIRouter(prefix="/api/audio", tags=["audio"])


def _get_audio_cache() -> AudioCache:
    from ..main import app_state
    return app_state['audio_cache']


@router.get("/{video_id}/status")
async def audio_status(video_id: str):
    cache = _get_audio_cache()
    return cache.status(video_id)


@router.post("/{video_id}/prepare")
async def prepare_audio(video_id: str):
    cache = _get_audio_cache()
    try:
        return await cache.prepare(video_id)
    except Exception as exc:
        raise HTTPException(500, f"Audio prepare failed: {exc}") from exc


@router.get("/{video_id}")
async def stream_audio(video_id: str, range: str | None = Header(default=None)):
    cache = _get_audio_cache()
    try:
        path, start, end, size, status = cache.open_range(video_id, range)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Audio is not ready") from exc

    content_type = mimetypes.guess_type(path.name)[0] or "audio/mp4"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=86400",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(
        cache.iter_file_range(path, start, end),
        status_code=status,
        media_type=content_type,
        headers=headers,
    )


@router.options("/{video_id}")
async def audio_options(video_id: str):
    return Response(headers={"Accept-Ranges": "bytes"})
