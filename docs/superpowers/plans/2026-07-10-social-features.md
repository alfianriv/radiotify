# Radiotify Social Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make listeners visible to each other (reactions, nicknames, dedications), open history/top-charts to the public with unlimited storage, add TV/karaoke mode, share links, and time-of-day mood scheduling.

**Architecture:** All realtime features ride the existing `/ws` WebSocket broadcast in `backend/app/main.py`. Identity is a client-side nickname (localStorage) sent with requests — no accounts. History persistence is SQLite (`play_history`), already indexed; the 10-row trim is simply removed. Mood scheduling seeds the existing random-track fallback by local hour. Share page is a tiny server-rendered HTML with OG tags reusing the YouTube thumbnail (no image generation dependency).

**Tech Stack:** FastAPI + WebSocket, Redis, SQLite, vanilla JS (Vite), pytest for backend unit tests.

**Verification baseline:** backend runs under pm2 (`radiotify-backend`), frontend is `vite build` served from `dist`. After each backend task: restart pm2 + curl. After frontend tasks: `npm run build`.

---

### Task 1: Unlimited play history (fix the 10-row trim)

**Files:**
- Modify: `backend/app/services/db.py:122-136`
- Create: `backend/tests/test_db.py`
- Create: `backend/tests/__init__.py` (empty)

- [x] **Step 1: Install pytest into venv** — `./venv/bin/pip install pytest` (dev-only, not added to requirements.txt)

- [x] **Step 2: Write failing test**

```python
# backend/tests/test_db.py
import os, tempfile
from app.services.db import Database

def make_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return Database(path)

def test_play_history_not_trimmed():
    db = make_db()
    for i in range(25):
        db.upsert_track(f'vid{i}', f'Title {i}', 'Artist')
        db.add_play_history(f'vid{i}')
    assert len(db.get_recently_played(limit=100)) == 25
```

- [x] **Step 3: Run** `./venv/bin/python -m pytest tests/test_db.py -v` from `backend/` — expect FAIL (returns 10)

- [x] **Step 4: Fix `add_play_history`** — delete the `DELETE FROM play_history WHERE id NOT IN (...)` block entirely; keep the INSERT.

- [x] **Step 5: Run test again** — expect PASS

- [x] **Step 6: Commit** `fix(history): stop trimming play_history to 10 rows`

### Task 2: Public history + top-tracks endpoint & tab

**Files:**
- Modify: `backend/app/services/db.py` (add `get_top_tracks`)
- Modify: `backend/app/api/radio.py` (add `GET /api/radio/history`, `GET /api/radio/top`)
- Modify: `backend/app/models.py` (add `TopTrackItem`, `TopTracksResponse`)
- Modify: `frontend/src/main.js` (History tab public; render top chart; drop admin gating)
- Modify: `frontend/index.html` (unhide history tab button)
- Test: `backend/tests/test_db.py` (add `test_get_top_tracks`)

- [x] **Step 1: Failing test for `get_top_tracks`**

```python
def test_get_top_tracks():
    db = make_db()
    db.upsert_track('a', 'Song A', 'X'); db.upsert_track('b', 'Song B', 'Y')
    for _ in range(3): db.add_play_history('a')
    db.add_play_history('b')
    top = db.get_top_tracks(days=7, limit=5)
    assert top[0]['video_id'] == 'a' and top[0]['play_count'] == 3
```

- [x] **Step 2: Implement `Database.get_top_tracks(days=7, limit=10)`** — GROUP BY video_id over `played_at > now - days*86400`, JOIN tracks, ORDER BY count DESC.

- [x] **Step 3: Run tests — PASS**

- [x] **Step 4: Add public endpoints in `radio.py`**: `GET /api/radio/history?limit=20` (reuses `db.get_history`) and `GET /api/radio/top?days=7&limit=10`. Keep `/api/admin/history` for compatibility.

- [x] **Step 5: Frontend** — always show `#tab-btn-history`; history tab renders "Top minggu ini" section (rank + title + play count) above recent list; fetch both public endpoints without adminFetch. Remove hidden-class toggling for history in `showAdminUI`/`adminLogout`.

- [x] **Step 6: Verify** — `curl localhost:8000/api/radio/history`, `curl localhost:8000/api/radio/top`, `npm run build`, pm2 restart, check tab in browser.

- [x] **Step 7: Commit** `feat(history): public history tab with weekly top chart`

### Task 3: Listener nickname identity

**Files:**
- Modify: `frontend/src/main.js` (nickname store + prompt modal reuse)
- Modify: `frontend/index.html` (nickname modal markup)
- Modify: `backend/app/models.py` (`QueueAddRequest.nickname: Optional[str]`)
- Modify: `backend/app/api/queue.py` (use nickname as `added_by`, IP-based rate-limit key)

- [x] **Step 1: Backend** — `QueueAddRequest` gains `nickname: Optional[str] = None`; in `add_to_queue`, `user_id = request.client.host` (real per-IP rate limit instead of global `"default"`), `added_by = sanitize(nickname)[:24] or 'Anon'`.

- [x] **Step 2: Frontend** — `getNickname()` from localStorage; settings menu item "Set Nickname" + first queue-add prompts modal (input, max 24 chars). Send `nickname` in queue POST body. Queue items render "oleh {added_by}" when source=user.

- [x] **Step 3: Verify via curl + browser; build.**

- [x] **Step 4: Commit** `feat(identity): lightweight nicknames for queue attribution`

### Task 4: Dedication messages

**Files:**
- Modify: `backend/app/models.py` (`QueueAddRequest.message`, `QueueItem.message`)
- Modify: `backend/app/api/queue.py` (accept + sanitize message ≤100 chars)
- Modify: `backend/app/radio_engine.py` (`_select_next_track`: merge `requested_by`/`dedication` into track meta when popping queue)
- Modify: `frontend/src/main.js` + `frontend/src/style.css` (dedication chip under artist on TRACK_CHANGED; message input in add-to-queue flow)

- [x] **Step 1: Backend models + queue endpoint** — pass through `message`.
- [x] **Step 2: Engine** — queue-pop branch merges `{'requested_by': item['added_by'], 'dedication': item['message']}` into resolved meta so it lands in `radio:state.meta` and `TRACK_CHANGED`.
- [x] **Step 3: Frontend** — after tapping a search result, small inline "+ pesan (opsional)" input; hero shows `💬 "{dedication}" — {requested_by}` chip when meta carries it.
- [x] **Step 4: Verify end-to-end: queue a song with message, wait for it to play, chip appears. Build + commit** `feat(dedication): request attribution and shoutout messages`

### Task 5: Live emoji reactions

**Files:**
- Modify: `backend/app/main.py` (handle client WS messages: `{"type":"REACTION","emoji":"🔥"}` → validate allowlist `🔥❤️🎉😂😭🙌` → broadcast `REACTION` with 1/sec per-connection throttle)
- Modify: `frontend/src/main.js` (reaction bar UI; send via WS; on REACTION event spawn floating emoji)
- Modify: `frontend/src/style.css` (float-up animation, reaction bar)
- Modify: `frontend/index.html` (reaction bar + layer container)

- [x] **Step 1: Backend WS handler** — parse JSON in the receive loop (currently ignored), validate, throttle via `time.monotonic()` per connection, `broadcast_event('REACTION', {'emoji': e})`.
- [x] **Step 2: Frontend** — fixed reaction bar (6 emoji buttons) bottom of hero; `ws.send(JSON.stringify(...))`; on event, spawn absolutely-positioned emoji at random x that floats up 2s then removes (transform/opacity only; respects reduced-motion).
- [x] **Step 3: Verify with two browser tabs. Build + commit** `feat(reactions): live emoji reactions via websocket`

### Task 6: TV / Karaoke mode

**Files:**
- Modify: `frontend/index.html` (settings item "TV Mode")
- Modify: `frontend/src/main.js` (toggle `.tv-mode` on lyrics overlay + requestFullscreen; show clock + listener count)
- Modify: `frontend/src/style.css` (tv-mode: larger lyric type, big album art header, clock)

- [x] **Step 1: Settings menu item** → `openLyricsOverlay()` + `document.documentElement.requestFullscreen()` + add `tv-mode` class; Esc/close removes both.
- [x] **Step 2: CSS** — `.lyrics-overlay.tv-mode`: max-width none, lyric active line ~40px, header shows clock (`tv-clock` span updated 1/min) and listener badge.
- [x] **Step 3: Verify in browser. Build + commit** `feat(tv): fullscreen karaoke/TV mode built on synced lyrics`

### Task 7: Mood scheduling by hour

**Files:**
- Create: `backend/app/services/moods.py` (pure function: hour → mood block)
- Modify: `backend/app/services/youtube.py` (`get_random_track(exclude, seed_queries=None)` uses seed query fallback)
- Modify: `backend/app/radio_engine.py` (pass current mood seeds on random fallback; include `mood` label in state/broadcast)
- Modify: `frontend/src/main.js` (mood label badge in hero)
- Test: `backend/tests/test_moods.py`

- [x] **Step 1: Failing test**

```python
from app.services.moods import get_mood
def test_mood_blocks():
    assert get_mood(6)['name'] == 'Morning Fresh'
    assert get_mood(22)['name'] == 'Night Vibes'
    assert get_mood(2)['name'] == 'Late Night'
```

- [x] **Step 2: Implement `moods.py`** — blocks: 05–10 Morning Fresh (`lagu pagi semangat`, `morning acoustic hits`), 10–16 Daytime Hits (`top hits indonesia`, `pop hits 2026`), 16–20 Golden Hour (`golden hour chill pop`, `lagu sore santai`), 20–24 Night Vibes (`lagu galau malam`, `late night r&b chill`), 00–05 Late Night (`sad indie midnight`, `lofi sleep chill`).
- [x] **Step 3: Engine + youtube service** — random fallback picks `random.choice(mood['queries'])` as search seed; `state['mood'] = mood['name']` set in `_play_track`, exposed via `/api/radio/state` and broadcasts.
- [x] **Step 4: Tests pass; curl state shows mood; frontend badge `☀/🌙 {mood}` near LIVE badge. Build + commit** `feat(mood): time-of-day mood scheduling and label`

### Task 8: Share page + share button

**Files:**
- Modify: `backend/app/api/radio.py` (`GET /share` HTML with OG meta: now-playing title/artist, `og:image` = track thumbnail, redirect script to `/`)
- Modify: `frontend/src/main.js` + `frontend/index.html` (share button in hero badges: `navigator.share` fallback copy-link of `{origin}/share`)

- [x] **Step 1: Backend `/share`** — HTMLResponse, meta og:title `▶ {title} — {artist} | Radiotify`, og:description "Dengar bareng — radio tersinkronisasi", og:image thumbnail, `<meta http-equiv="refresh" content="0;url=/">` for humans.
- [x] **Step 2: Frontend share button** (icon next to lyrics button) → `navigator.share({url})` or clipboard + toast "Link disalin".
- [x] **Step 3: Verify `curl localhost:8000/share`. Build + commit** `feat(share): now-playing share page with OG tags`

### Task 9: Ship

- [x] **Step 1:** Full test run `./venv/bin/python -m pytest tests/ -v` — all pass
- [x] **Step 2:** `npm run build`, `pm2 restart radiotify-backend radiotify-frontend`
- [x] **Step 3:** Smoke: state, history, top, share endpoints; two-tab reaction test
- [x] **Step 4:** Push `feature/update`
