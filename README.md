# 📻 Radiotify

A synchronized web radio — all listeners hear the same song at the same position, in real time.

Built as a portfolio project. Streams music via YouTube, pre-encodes HLS segments per track, and keeps every client in sync using server-side timestamps and WebSocket events.

> **Note:** This project operates in a grey area regarding copyright law and platform terms of service. It may be taken offline at any time.

---

## Features

- 🎵 24/7 radio engine with deterministic timeline
- 🔄 Real-time sync across all listeners via WebSocket
- 📡 HLS adaptive streaming (pre-encoded per track, no live lag)
- 🎛️ Admin panel — skip, force play, queue management, maintenance mode
- 🗳️ Vote-to-skip for listeners
- 🔍 YouTube Music search & auto-recommendations
- 📱 PWA-ready (installable, lock screen controls)
- 🛠️ Maintenance mode — pauses engine, freezes playback position, resumes seamlessly

---

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python, FastAPI, WebSocket |
| Streaming | ffmpeg, HLS (pre-encoded VOD segments) |
| Music | yt-dlp, ytmusicapi |
| State | Redis |
| Database | SQLite |
| Frontend | Vanilla JS, HLS.js, Vite |

---

## Project Structure

```
radiotify/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app, WebSocket, middleware
│   │   ├── radio_engine.py      # Core radio loop, track transitions
│   │   ├── models.py            # Pydantic models
│   │   ├── config.py            # Settings from .env
│   │   ├── api/
│   │   │   ├── radio.py         # /api/radio/* endpoints
│   │   │   ├── queue.py         # /api/queue/* endpoints
│   │   │   ├── admin.py         # /api/admin/* endpoints
│   │   │   ├── audio.py         # /api/audio/* endpoints
│   │   │   ├── live.py          # /api/live/stream (MP3 fallback)
│   │   │   └── hls_stream.py    # /api/live/hls/* endpoints
│   │   └── services/
│   │       ├── db.py            # SQLite (history, config, stats)
│   │       ├── redis_state.py   # Redis (radio state, queue, votes)
│   │       ├── youtube.py       # yt-dlp + ytmusicapi
│   │       ├── audio_cache.py   # Audio file download & cache
│   │       └── hls_live_worker.py # ffmpeg HLS pre-encoder
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── main.js              # App logic, WS client, playback
    │   └── style.css            # UI styles
    ├── index.html
    └── vite.config.js
```

---

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- Redis 7+
- ffmpeg

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set ADMIN_PASSWORD and JWT_SECRET_KEY

redis-server &
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend

```bash
cd frontend
npm install
npm run build        # production build → dist/
# or
npm run dev          # dev server with HMR
```

The backend serves the frontend `dist/` folder automatically at `/`.

---

## Environment Variables

See `backend/.env.example` for all options.

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `REDIS_HOST` | Redis host | `localhost` |
| `REDIS_PORT` | Redis port | `6379` |
| `ADMIN_PASSWORD` | Admin panel password | — |
| `JWT_SECRET_KEY` | JWT signing secret | — |
| `CROSSFADE_DURATION_MS` | Crossfade duration | `800` |

---

## API Overview

### Public

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/radio/state` | Current track, timestamps, maintenance status |
| `POST` | `/api/radio/vote-skip` | Vote to skip current track |
| `GET` | `/api/queue` | Current queue |
| `POST` | `/api/queue` | Add track to queue |
| `GET` | `/api/queue/search?q=` | Search YouTube Music |
| `GET` | `/api/live/hls/{id}/playlist.m3u8` | HLS playlist for track |
| `WS` | `/ws` | Real-time events |

### Admin (JWT required)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/admin/login` | Get JWT token |
| `POST` | `/api/admin/skip` | Skip current track |
| `POST` | `/api/admin/force-play` | Force play by video ID |
| `POST` | `/api/admin/maintenance` | Toggle maintenance mode |
| `GET` | `/api/admin/history` | Play history |

### WebSocket Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `RADIO_STATE` | server → client | Full state on connect |
| `TRACK_CHANGED` | server → client | New track started |
| `QUEUE_UPDATED` | server → client | Queue changed |
| `MAINTENANCE` | server → client | Maintenance toggled |
| `PLAYBACK_RESUMED` | server → client | First client confirmed playback after maintenance |
| `LISTENER_COUNT` | server → client | Listener count update |
| `SKIP_VOTES` | server → client | Vote skip progress |

---

## Maintenance Mode

When enabled:
- Radio engine pauses (no track transitions)
- HLS encoding and serving stops
- Playback position is frozen server-side
- All clients show a maintenance overlay in real time

When disabled:
- Engine resumes from the exact paused position
- HLS re-encodes current track
- All clients resume playback automatically
- Overlay hides once audio starts playing

Admin can toggle maintenance from the overlay itself without leaving the page.
