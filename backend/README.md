# Radiotify Backend

Python FastAPI backend for synchronized web radio.

## Features

- 24/7 radio engine with deterministic timeline
- YouTube Music integration (search, recommendations)
- Queue system with priority logic
- Admin controls (skip, force play, clear queue)
- WebSocket for real-time events
- Redis state management
- SQLite metadata caching

## Requirements

- Python 3.11+
- Redis 7+

## Setup

1. **Install dependencies:**

```bash
cd backend
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. **Configure environment:**

```bash
cp .env.example .env
# Edit .env with your settings
```

3. **Start Redis:**

```bash
redis-server
```

4. **Run backend:**

```bash
python -m app.main
```

Backend runs on `http://localhost:8000`

## API Endpoints

### Radio

- `GET /radio/state` - Get current radio state
- `GET /radio/current` - Get current track metadata
- `GET /radio/next` - Get next track (if preloaded)
- `GET /radio/history` - Get play history
- `GET /radio/search?q=query` - Search tracks

### Queue

- `GET /queue/` - Get current queue
- `POST /queue/add` - Add track to queue
- `GET /queue/length` - Get queue length

### Admin

- `POST /admin/login` - Admin login (get JWT token)
- `POST /admin/skip` - Skip current track
- `POST /admin/force-play` - Force play specific track
- `POST /admin/clear-queue` - Clear entire queue

### WebSocket

- `WS /ws` - Real-time events (TRACK_CHANGED, QUEUE_UPDATED)

## Architecture

```
app/
├── main.py              # FastAPI app + WebSocket
├── radio_engine.py      # Core radio logic
├── models/              # Pydantic models
├── api/                 # REST endpoints
│   ├── radio.py
│   ├── queue.py
│   └── admin.py
└── services/            # External services
    ├── redis.py         # Redis client
    ├── youtube.py       # yt-dlp + ytmusicapi
    └── db.py            # SQLite cache
```

## Development

```bash
# Run with auto-reload
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Check logs
tail -f logs/radiotify.log
```

## Environment Variables

See `.env.example` for all configuration options.
