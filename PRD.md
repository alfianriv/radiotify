# 📻 SyncRadio --- Product Requirement Document (PRD)

## 1. Overview

### Product Name

**SyncRadio** (working name)

### Product Type

Web-based synchronized online radio.

### Core Idea

A web radio that: - Always runs (24/7) - Independent from users - All
listeners hear the **exact same playback time** - Infinite auto
playback - Users can add songs to queue - Admin has full control

Behavior must mimic **real FM radio broadcast**.

------------------------------------------------------------------------

## 2. Product Goals

### Primary Goals

1.  Radio runs continuously even with zero users.
2.  All users remain synchronized.
3.  Seamless song transitions.
4.  Fully automatic playback.
5.  Admin override anytime.

### Non Goals

-   No multi-room radio
-   No personal playlists
-   No per-user playback
-   No backend audio streaming
-   No global pause

Radio never pauses.

------------------------------------------------------------------------

## 3. Success Metrics

  Metric           Target
  ---------------- ----------------
  Playback drift   \< 500 ms
  Transition gap   0 ms perceived
  Uptime           99.9%
  Join latency     \< 1.5s
  Queue delay      \< 100ms

------------------------------------------------------------------------

## 4. User Roles

### Listener

-   Listen to radio
-   View current song
-   Add songs to queue

### Admin

-   Full playback control
-   Force play
-   Skip track
-   Clear queue
-   Lock queue

------------------------------------------------------------------------

## 5. Core User Experience

### Listener Flow

1.  User opens website
2.  Client fetches radio state
3.  Player seeks to radio position
4.  Playback instantly syncs
5.  User may add queue

No manual play required after sync.

### Admin Flow

Admin panel: - View queue - View history - Skip track - Force play -
Remove queue item

All actions realtime.

------------------------------------------------------------------------

## 6. Functional Requirements

### 6.1 Radio Engine

Radio must: - Run continuously - Be deterministic - Use server time

#### Radio State

``` ts
RadioState {
  currentTrackId
  nextTrackId
  startedAtEpochMs
  transitionAtEpochMs
}
```

Rules: - No pause state - Next track known before end - Server restart
must not stop radio

------------------------------------------------------------------------

### 6.2 Playback Decision Logic

Priority: 1. Admin forced track 2. User queue 3. YouTube Music Up Next
4. Random track

Rules: - Avoid same artist consecutively - Avoid recently played tracks

------------------------------------------------------------------------

### 6.3 Infinite Playback

Radio continues even if: - Queue empty - No listeners

------------------------------------------------------------------------

### 6.4 Synchronization System

Single source of truth: **Server Clock**

#### Sync API

    GET /radio/state

Response:

``` json
{
  "trackId": "...",
  "startedAt": "...",
  "serverTime": "..."
}
```

Client calculates:

    seek = radioNow - startedAt

#### Drift Correction

Client must: - Check drift every 5--10s - Adjust playbackRate - Hard
seek if drift \> 1.5s

------------------------------------------------------------------------

### 6.5 Smooth Transition (Gapless)

Requirements: - Preload next track - Dual player strategy - Crossfade
transition

Rules: - Next track sent ≥20s before end - Default crossfade 800ms -
Transition synchronized across clients

------------------------------------------------------------------------

### 6.6 Queue System

``` ts
QueueItem {
  id
  trackId
  addedBy
  source: USER | ADMIN
}
```

Constraints: - Per-user limit - Deduplicate songs - FIFO order

------------------------------------------------------------------------

### 6.7 Admin Control Panel

Admin capabilities: - Skip track - Force play - Remove queue item -
Clear queue - Lock / unlock queue

Admin overrides automation.

------------------------------------------------------------------------

### 6.8 Realtime Events

WebSocket Events:

    TRACK_CHANGED
    QUEUE_UPDATED
    ADMIN_ACTION

No continuous timer broadcasting.

------------------------------------------------------------------------

### 6.9 Music Provider Integration

    MusicProvider
     ├── search()
     ├── getTrack()
     ├── getUpNext()
     └── random()

Source: YouTube Music.

------------------------------------------------------------------------

## 7. Non‑Functional Requirements

### Performance

-   Support ≥10k concurrent listeners
-   Stateless API scaling

### Reliability

-   Auto resume after restart
-   Worker crash must not reset playback

### Scalability

Frontend streams directly from YouTube. Backend acts only as control
plane.

### Latency

Radio state fetch \< 200ms.

------------------------------------------------------------------------

## 8. System Architecture

    Frontend (Dual YT Player)
            │
         WebSocket
            │
    API Server
            │
    Radio Engine Worker
            │
    Redis (Clock + Queue)
            │
    YouTube Music Provider

------------------------------------------------------------------------

## 9. Data Storage

### Redis Runtime Keys

    radio:state
    radio:queue
    radio:history

### Database (Optional)

-   tracks
-   play_history
-   admin_actions

------------------------------------------------------------------------

## 10. Edge Cases

-   User joins mid-song → auto seek
-   Server restart → resume timeline
-   Next track load failure → random fallback
-   No recommendation → random song
-   Client lag → drift correction

------------------------------------------------------------------------

## 11. Security

-   Admin authentication
-   Queue rate limiting
-   Anti-spam protections

------------------------------------------------------------------------

## 12. Observability

Metrics: - Average drift - Transition success rate - Queue usage - Radio
uptime

Logs: - Track changes - Admin overrides - Recommendation failures

------------------------------------------------------------------------

## 13. Future Enhancements (Out of Scope)

-   Voting skip
-   AI DJ
-   Genre scheduling
-   Mobile apps
-   Multi-channel radio

------------------------------------------------------------------------

## 14. Technical Risks

### YouTube API Limitation

Mitigation: - Metadata caching - Random fallback

### Browser Autoplay Restriction

Mitigation: - Initial user interaction bootstrap

### Player Buffering

Mitigation: - Dual player preload strategy

------------------------------------------------------------------------

## 15. Definition of Done

System complete when:

-   Radio runs without users
-   All listeners sync (\<500ms drift)
-   Seamless transitions
-   Admin override realtime
-   Restart does not stop playback
