"""Radiotify — Synchronized Web Radio. Main FastAPI application."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import config
from .services.db import Database
from .services.redis_state import RedisState
from .services.youtube import YouTubeMusicService
from .radio_engine import RadioEngine
from .api import radio, queue, admin, audio, live
from .services.audio_cache import AudioCache
from .services.hls_live_worker import HlsLiveWorker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)
logger = logging.getLogger(__name__)

# Global app state
app_state: Dict[str, Any] = {}

# WebSocket connections
ws_connections: Set[WebSocket] = set()

# Background task references
_engine_task = None
_hls_task = None


async def broadcast_event(event: str, data: Any):
    """Broadcast event to all connected WebSocket clients."""
    import json
    message = json.dumps({'event': event, 'data': data})
    disconnected = set()
    for ws in ws_connections:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    ws_connections.difference_update(disconnected)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global _engine_task, _hls_task

    # Initialize services
    db = Database(config.DB_PATH)
    redis_state = RedisState()
    youtube = YouTubeMusicService(db)
    engine = RadioEngine(redis_state, youtube, db)
    audio_cache = AudioCache()
    hls_worker = HlsLiveWorker()
    engine.set_broadcast_fn(broadcast_event)

    app_state['db'] = db
    app_state['redis'] = redis_state
    app_state['youtube'] = youtube
    app_state['engine'] = engine
    app_state['audio_cache'] = audio_cache
    app_state['hls_worker'] = hls_worker

    logger.info("🚀 Starting Radio Engine...")
    _engine_task = asyncio.create_task(engine.start())

    logger.info("🎙 Starting HLS Live Worker...")
    _hls_task = asyncio.create_task(hls_worker.start(audio_cache))

    yield

    # Shutdown
    logger.info("🛑 Shutting down...")
    await engine.stop()
    await hls_worker.stop()
    for task in (_engine_task, _hls_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Radiotify", version="0.1.0", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def maintenance_mode_check(request, call_next):
    """During maintenance, block API endpoints but keep frontend and status check accessible."""
    from fastapi.responses import JSONResponse
    db = app_state.get('db')
    if db and db.is_maintenance_mode():
        path = request.url.path
        # Always allow: admin API, maintenance status check, and all frontend assets
        if (
            path.startswith('/api/admin')
            or path in ('/api/radio/state', '/api/radio/playback-resumed')
            or not path.startswith('/api')
        ):
            return await call_next(request)
        # Block all other API endpoints
        return JSONResponse(
            status_code=503,
            content={
                "error": "Service Unavailable",
                "message": db.get_maintenance_message(),
                "maintenance": True
            }
        )
    return await call_next(request)


@app.middleware("http")
async def no_stale_pwa_shell(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in {"/", "/index.html", "/sw.js", "/manifest.webmanifest"}:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Register API routes
app.include_router(radio.router)
app.include_router(queue.router)
app.include_router(admin.router)
app.include_router(audio.router)
app.include_router(live.router)

# HLS/DASH adaptive streaming with crossfade
try:
    from .api import hls_stream
    app.include_router(hls_stream.router)
except ImportError:
    logger.warning("HLS streaming module not available")



# ── WebSocket ───────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time events."""
    await websocket.accept()
    ws_connections.add(websocket)
    logger.info(f"WebSocket connected. Total: {len(ws_connections)}")
    count = len(ws_connections)
    app_state['db'].record_listener_count(count)
    await broadcast_event('LISTENER_COUNT', {'count': count})

    # Send current state on connect
    try:
        import json, time
        db = app_state['db']
        redis: RedisState = app_state['redis']

        # Always send maintenance status first
        if db.is_maintenance_mode():
            await websocket.send_text(json.dumps({
                'event': 'MAINTENANCE',
                'data': {
                    'enabled': True,
                    'message': db.get_maintenance_message(),
                },
            }))

        state = redis.get_radio_state()
        if state:
            state['server_time_ms'] = time.time() * 1000
            state['listeners'] = len(ws_connections)
            await websocket.send_text(json.dumps({
                'event': 'RADIO_STATE',
                'data': state,
            }))
    except Exception:
        pass

    try:
        while True:
            # Keep connection alive, receive any client messages
            data = await websocket.receive_text()
            # Client messages are ignored for now (all control via REST)
    except WebSocketDisconnect:
        ws_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(ws_connections)}")
        count = len(ws_connections)
        app_state['db'].record_listener_count(count)
        await broadcast_event('LISTENER_COUNT', {'count': count})
    except Exception:
        ws_connections.discard(websocket)
        count = len(ws_connections)
        app_state['db'].record_listener_count(count)
        await broadcast_event('LISTENER_COUNT', {'count': count})


# ── Static Files (Frontend) ────────────────────────────────

frontend_dist = os.path.join(os.path.dirname(__file__), '..', '..', 'frontend', 'dist')
frontend_static = os.path.join(os.path.dirname(__file__), '..', 'static')

# Serve frontend dist if it exists (production), otherwise serve backend static
if os.path.isdir(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
elif os.path.isdir(frontend_static):
    app.mount("/static", StaticFiles(directory=frontend_static), name="static")
