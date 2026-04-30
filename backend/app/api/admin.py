"""Admin API endpoints."""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Request

import jwt
from ..models import (
    AdminLoginRequest, AdminLoginResponse,
    AdminForcePlayRequest, AdminRemoveQueueRequest,
    HistoryResponse, HistoryItem,
)
from ..config import config
from ..services.redis_state import RedisState
from ..services.db import Database

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


def _get_redis() -> RedisState:
    from ..main import app_state
    return app_state['redis']


def _get_db() -> Database:
    from ..main import app_state
    return app_state['db']


def _get_engine():
    from ..main import app_state
    return app_state['engine']


def create_token() -> str:
    payload = {
        'role': 'admin',
        'exp': datetime.utcnow() + timedelta(hours=24),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm='HS256')


def verify_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=['HS256'])
        return payload.get('role') == 'admin'
    except jwt.ExpiredSignatureError:
        return False
    except jwt.InvalidTokenError:
        return False


async def require_admin(request: Request, token: str = ""):
    """Dependency for admin endpoints. Accepts token via query param or Authorization header."""
    # Try query param first, then Authorization header
    if not token:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Token required")
    if not verify_token(token):
        raise HTTPException(403, "Invalid or expired token")
    return True


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(req: AdminLoginRequest):
    """Login to admin panel."""
    if req.password != config.ADMIN_PASSWORD:
        raise HTTPException(401, "Invalid password")
    token = create_token()
    return AdminLoginResponse(token=token)


@router.post("/skip")
async def skip_track(authorized: bool = Depends(require_admin)):
    """Skip current track."""
    engine = _get_engine()
    db: Database = _get_db()
    state = _get_redis().get_radio_state()
    if state:
        db.log_admin_action('skip', state.get('current_track_id'))
    await engine.admin_skip()
    return {"ok": True}


@router.post("/force-play")
async def force_play(req: AdminForcePlayRequest, authorized: bool = Depends(require_admin)):
    """Force play a specific track."""
    engine = _get_engine()
    db: Database = _get_db()
    db.log_admin_action('force_play', req.video_id)
    await engine.admin_force_play(req.video_id)
    return {"ok": True}


@router.delete("/queue/{index}")
async def remove_queue_item(index: int, authorized: bool = Depends(require_admin)):
    """Remove item from queue."""
    engine = _get_engine()
    db: Database = _get_db()
    removed = await engine.admin_remove_queue_item(index)
    if not removed:
        raise HTTPException(404, "Queue item not found")
    db.log_admin_action('remove_queue_item', removed.get('video_id'))
    return {"ok": True, "removed": removed}


@router.delete("/queue")
async def clear_queue(authorized: bool = Depends(require_admin)):
    """Clear entire queue."""
    engine = _get_engine()
    db: Database = _get_db()
    db.log_admin_action('clear_queue')
    await engine.admin_clear_queue()
    return {"ok": True}


@router.post("/queue/lock")
async def lock_queue(authorized: bool = Depends(require_admin)):
    """Lock the queue (prevent new additions)."""
    engine = _get_engine()
    db: Database = _get_db()
    db.log_admin_action('lock_queue')
    await engine.admin_lock_queue(True)
    return {"ok": True}


@router.post("/queue/unlock")
async def unlock_queue(authorized: bool = Depends(require_admin)):
    """Unlock the queue."""
    engine = _get_engine()
    db: Database = _get_db()
    db.log_admin_action('unlock_queue')
    await engine.admin_lock_queue(False)
    return {"ok": True}


@router.get("/history", response_model=HistoryResponse)
async def get_history(limit: int = 20, authorized: bool = Depends(require_admin)):
    """Get play history."""
    db: Database = _get_db()
    items = db.get_history(limit=limit)
    return HistoryResponse(history=[HistoryItem(**h) for h in items])


# ── Maintenance Mode ────────────────────────────────────────

@router.get("/maintenance")
async def get_maintenance(authorized: bool = Depends(require_admin)):
    """Get current maintenance mode status."""
    db: Database = _get_db()
    return {
        "enabled": db.is_maintenance_mode(),
        "message": db.get_maintenance_message()
    }


@router.post("/maintenance")
async def set_maintenance(
    enabled: bool,
    message: Optional[str] = None,
    authorized: bool = Depends(require_admin)
):
    """Enable or disable maintenance mode."""
    logger.info(f"Maintenance mode request: enabled={enabled}")
    try:
        db: Database = _get_db()
        engine = _get_engine()
        db.set_config('maintenance_mode', 'true' if enabled else 'false')
        if message is not None:
            db.set_config('maintenance_message', message)
        db.log_admin_action('maintenance_mode', details=f'enabled={enabled}')

        # Freeze / unfreeze radio state timestamps
        from app.main import app_state, broadcast_event
        redis = app_state.get('redis')
        state = redis.get_radio_state() if redis else None

        if enabled and state:
            # Store elapsed at pause time so resume can restore exact position
            now_ms = int(time.time() * 1000)
            paused_elapsed_ms = max(0, now_ms - int(state.get('started_at_ms', now_ms)))
            state['paused_elapsed_ms'] = paused_elapsed_ms
            state['paused_at_ms'] = now_ms
            redis.set_radio_state(state)
            logger.info(f"Maintenance ON: paused at elapsed={paused_elapsed_ms}ms")

        await broadcast_event('MAINTENANCE', {
            'enabled': enabled,
            'message': db.get_maintenance_message()
        })

        if not enabled:
            engine = _get_engine()
            await engine.resume_after_maintenance()
        return {"ok": True, "enabled": enabled, "message": db.get_maintenance_message()}
    except Exception as e:
        logger.exception(f"Error setting maintenance mode: {e}")
        raise HTTPException(500, str(e))
