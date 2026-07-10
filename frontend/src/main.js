/**
 * Radiotify — Main Client v2
 * Tabs UI, Admin Panel, Search, History, Dynamic Title
 */
import './style.css';

const API_BASE = '/api';
let hlsPlayer = null;
let hlsStreamLoaded = false;
const DRIFT_CHECK_MS = 3000;
const DRIFT_THRESHOLD_MS = 1500;   // less aggressive on mobile
const DRIFT_SOFT_LIMIT_MS = 5000;  // only hard-seek if really far off
const CROSSFADE_MS = 3000;

// ── State ──────────────────────────────────────────────────

let audioA = null, audioB = null;
let activeAudio = 'A';
let playbackMode = 'audio';
localStorage.setItem('playback_mode', playbackMode);
let radioState = null;
let ws = null;
let driftInterval = null;
let crossfadePromise = null;
let crossfadeTargetId = null;
let adminToken = localStorage.getItem('admin_token') || null;
let isAdmin = false;
let deferredInstallPrompt = null;
let maintenanceStartedAt = null; // timestamp when maintenance was activated
let lyricsData = null; // [{time_ms, text}, ...]
let lyricsActive = false;
let lyricsRafId = null;
let currentLyricsVideoId = null;
let wakeLock = null; // Screen Wake Lock
const isStandalonePwa = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
const isIosDevice = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

// ── Elements ───────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const elTrackTitle = $('track-title');
const elTrackArtist = $('track-artist');
const elAlbumArt = $('album-art');
const elProgressFill = $('progress-fill');
const elCurrentTime = $('current-time');
const elTotalTime = $('total-time');
const elQueueList = $('queue-list');
const elSearchInput = $('search-input');
const elSearchResults = $('search-results');
const elHeroBg = $('hero-bg');
const elListenerNum = $('listener-num');
const elAdminBadge = $('admin-badge');
const elAdminBar = $('admin-bar');
const elQueueAdminActions = $('queue-admin-actions');
const elHistoryList = $('history-list');
const elPlaybackStatus = $('playback-status');

// ── Audio Players ──────────────────────────────────────────

async function initPlayers() {
  initAudioPlayers();
}

function initAudioPlayers() {
  audioA = new Audio();
  audioB = new Audio();
  [audioA, audioB].forEach((audio) => {
    audio.preload = 'auto';
    audio.crossOrigin = 'anonymous';
    audio.volume = 0;
    // Sync MediaSession playback state with actual audio
    audio.addEventListener('play', () => {
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    });
    audio.addEventListener('pause', () => {
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    });
  });
}

// ── Playback ───────────────────────────────────────────────

function getActiveAudio() { return activeAudio === 'A' ? audioA : audioB; }
function getInactiveAudio() { return activeAudio === 'A' ? audioB : audioA; }
function audioUrlFor(state) { return state?.audio_url || (state?.track_id ? `${API_BASE}/audio/${state.track_id}` : null); }
function liveUrl() { return `${API_BASE}/live/stream`; }
function hlsLiveUrl(trackId) {
  if (trackId) return `${API_BASE}/live/hls/${trackId}/playlist.m3u8`;
  // Fallback: use current radioState track_id
  const tid = radioState?.track_id;
  return tid ? `${API_BASE}/live/hls/${tid}/playlist.m3u8` : `${API_BASE}/live/hls/master.m3u8`;
}
function isLiveStreamSrc(src = '') { return src.includes('/api/live/hls/') || src.includes('/api/live/stream'); }
function isNativeAudioMode() { return true; }
function isAudioStreamMode() { return true; }
function setPlaybackStatus(message, kind = 'ok') {
  if (!elPlaybackStatus) return;
  elPlaybackStatus.textContent = message;
  elPlaybackStatus.dataset.kind = kind;
}

function fadeVolumes(apply, durationMs = CROSSFADE_MS) {
  const start = performance.now();
  return new Promise((resolve) => {
    const tick = () => {
      const progress = Math.min(1, (performance.now() - start) / durationMs);
      const fadeIn = Math.sin(progress * Math.PI / 2);
      const fadeOut = Math.cos(progress * Math.PI / 2);
      apply(fadeIn, fadeOut);
      if (progress < 1) requestAnimationFrame(tick);
      else resolve();
    };
    requestAnimationFrame(tick);
  });
}

async function playAudioTrack(state, seekMs = 0) {
  const url = audioUrlFor(state);
  if (!url || state.audio_status !== 'ready') return false;
  const audio = getActiveAudio();
  const inactive = getInactiveAudio();
  try {
    inactive.pause();
    inactive.volume = 0;
    audio.src = url;
    audio.currentTime = Math.max(0, seekMs / 1000);
    audio.volume = 1;
    await audio.play();
    if (getActivePlayer()?.stopVideo) getActivePlayer().stopVideo();
    setPlaybackStatus('Audio Stream active', 'ok');
    return true;
  } catch (err) {
    console.error('Audio mode error:', err);
    setPlaybackStatus('Audio Stream error', 'err');
    return false;
  }
}

async function playLiveStream(forceReload = false, trackId = null, seekMs = 0) {
  stopDriftCorrection();
  const audio = getActiveAudio();
  const inactive = getInactiveAudio();
  inactive.pause();
  inactive.volume = 0;
  audio.volume = 1;
  audio.preload = 'auto';

  const HlsCtor = window.Hls;
  const hlsUrl = hlsLiveUrl(trackId);

  // ── HLS.js path ──────────────────────────────────────────
  if (HlsCtor?.isSupported()) {
    // Only destroy if attached to wrong audio element — NOT on track change
    // Destroying on track change clears the buffer and causes re-buffering stutter
    if (hlsPlayer && hlsPlayer._media && hlsPlayer._media !== audio) {
      hlsPlayer.destroy();
      hlsPlayer = null;
      hlsStreamLoaded = false;
    }

    if (!hlsPlayer) {
      hlsPlayer = new HlsCtor({
        enableWorker: true,
        lowLatencyMode: false,
        // Buffer tuned for mobile: fast start, prefetch next segments
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
        backBufferLength: 10,
        startFragPrefetch: true,
        xhrSetup: (xhr) => { xhr.timeout = 15000; },
      });
      hlsPlayer.attachMedia(audio);
      hlsStreamLoaded = false;



      // Fatal error → try refreshing state first, then fall through to MP3
      hlsPlayer.on(HlsCtor.Events.ERROR, (_, data) => {
        if (data.fatal) {
          console.warn('HLS fatal error:', data);
          hlsPlayer?.destroy();
          hlsPlayer = null;
          hlsStreamLoaded = false;
          // Fetch fresh state — track may have changed since we started loading
          fetch(`${API_BASE}/radio/state`).then(r => r.ok ? r.json() : null).then(state => {
            const freshTrackId = state?.track_id || state?.current_track_id;
            const currentTrackId = radioState?.track_id || radioState?.current_track_id;
            if (freshTrackId && freshTrackId !== currentTrackId) {
              // Track changed — WS will handle playback, just update state
              radioState = state;
            } else {
              // Same track but HLS failed — fall back to MP3
              const mp3Url = `${liveUrl()}?t=${Date.now()}`;
              audio.src = mp3Url;
              audio.play().catch(() => {});
              setPlaybackStatus('Live MP3 stream (HLS fallback)', 'ok');
            }
          }).catch(() => {
            const mp3Url = `${liveUrl()}?t=${Date.now()}`;
            audio.src = mp3Url;
            audio.play().catch(() => {});
            setPlaybackStatus('Live MP3 stream (HLS fallback)', 'ok');
          });
        }
      });
    }

    try {
      // Load source only once (or on explicit forceReload / track change)
      if (!hlsStreamLoaded || forceReload) {
        // Use stopLoad() before loadSource() when reusing the same instance
        // This is the correct HLS.js API for switching tracks without destroying
        if (hlsStreamLoaded && forceReload) hlsPlayer.stopLoad();
        hlsPlayer.loadSource(hlsUrl);
        hlsStreamLoaded = true;

        // Wait for manifest, seek to correct position, then play
        await new Promise((resolve) => {
          const timeout = setTimeout(resolve, 8000); // fallback if manifest never fires
          hlsPlayer.once(HlsCtor.Events.MANIFEST_PARSED, () => {
            clearTimeout(timeout);
            if (seekMs > 0) {
              audio.currentTime = Math.max(0, seekMs / 1000);
            }
            resolve();
          });
        });
      } else if (seekMs > 0) {
        // Already loaded — just seek
        audio.currentTime = Math.max(0, seekMs / 1000);
      }
      await audio.play();
      setPlaybackStatus('HLS stream', 'ok');
      startDriftCorrection();
      return true;
    } catch (err) {
      console.warn('HLS play() failed, falling back to MP3:', err);
      hlsPlayer?.destroy();
      hlsPlayer = null;
      hlsStreamLoaded = false;
      // fall through to MP3
    }
  }

  // ── Native HLS (Safari) ───────────────────────────────────
  if (audio.canPlayType('application/vnd.apple.mpegurl')) {
    try {
      if (!audio.src.includes(trackId || 'playlist') || forceReload) {
        audio.src = hlsUrl;
        audio.load();
      }
      if (seekMs > 0) {
        await new Promise((resolve) => {
          const timeout = setTimeout(resolve, 8000);
          audio.addEventListener('loadedmetadata', () => {
            clearTimeout(timeout);
            audio.currentTime = Math.max(0, seekMs / 1000);
            resolve();
          }, { once: true });
        });
      }
      await audio.play();
      setPlaybackStatus('Native HLS stream', 'ok');
      startDriftCorrection();
      return true;
    } catch (err) {
      console.warn('Native HLS failed, falling back to MP3:', err);
    }
  }

  // ── MP3 fallback ──────────────────────────────────────────
  try {
    const mp3Url = `${liveUrl()}?t=${Date.now()}`;
    audio.src = mp3Url;
    await audio.play();
    setPlaybackStatus('Live MP3 stream', 'ok');
    startDriftCorrection();
    return true;
  } catch (err) {
    if (err.name === 'NotAllowedError') {
      setPlaybackStatus('Click anywhere to start', 'err');
      const overlay = $('tap-overlay');
      overlay.classList.remove('hidden');
      overlay.addEventListener('click', () => {
        overlay.classList.add('hidden');
        audio.play().catch(() => {});
      }, { once: true });
    } else {
      console.error('Live stream error:', err);
      setPlaybackStatus('Stream error — tap to retry', 'err');
    }
    startDriftCorrection();
    return false;
  }
}

async function crossfadeAudioTo(state, seekMs = 0) {
  const url = audioUrlFor(state);
  if (!url || state.audio_status !== 'ready') return false;
  const current = getActiveAudio();
  if ((current?.src || '').includes(url)) {
    current.currentTime = Math.max(0, seekMs / 1000);
    current.volume = 1;
    if (current.paused) await current.play();
    return true;
  }
  const nextKey = activeAudio === 'A' ? 'B' : 'A';
  const next = nextKey === 'A' ? audioA : audioB;
  try {
    next.src = url;
    next.currentTime = Math.max(0, seekMs / 1000);
    next.volume = 0;
    await next.play();
    await fadeVolumes((fadeIn, fadeOut) => {
      next.volume = fadeIn;
      current.volume = fadeOut;
    }, CROSSFADE_MS);
    current.pause();
    current.volume = 0;
    next.volume = 1;
    activeAudio = nextKey;
    return true;
  } catch (err) {
    console.error('Audio crossfade error:', err);
    setPlaybackStatus('Audio Stream crossfade error', 'err');
    return false;
  }
}

function getActiveMediaId() {
  if (isNativeAudioMode()) return getActiveAudio()?.src || '';
  return getActivePlayer()?.getVideoData?.()?.video_id || '';
}

async function playState(state, seekMs = 0, allowCrossfade = false) {
  playbackMode = 'audio';
  localStorage.setItem('playback_mode', playbackMode);
  // REST API uses track_id, WebSocket RADIO_STATE uses current_track_id
  const trackId = state?.track_id || state?.current_track_id || null;
  // forceReload=true so HLS.js always fetches fresh playlist for current track
  return playLiveStream(true, trackId, seekMs);
}

// ── Sync & Drift ───────────────────────────────────────────

async function fetchRadioState() {
  try {
    const res = await fetch(`${API_BASE}/radio/state`);
    return await res.json();
  } catch { return null; }
}

async function syncToRadio(videoId = null, elapsedMs = null) {
  // Drift correction ONLY — never triggers playback.
  // If audio is not playing, do nothing and let WS events handle playback start.
  const audio = getActiveAudio();
  if (!audio || audio.paused) return;

  let state = radioState;
  if (!state) return; // no state yet, skip

  const nowMs = Date.now();
  const computedElapsed = nowMs - (state?.started_at_ms || nowMs);
  const resolvedElapsed = elapsedMs !== null ? elapsedMs : computedElapsed;
  const durationMs = (state?.transition_at_ms || 0) - (state?.started_at_ms || 0);
  const displayDurationMs = (state?.meta?.duration_seconds || state?.current_track_meta?.duration_seconds || state?.duration_seconds)
    ? (state?.meta?.duration_seconds || state?.current_track_meta?.duration_seconds || state?.duration_seconds) * 1000
    : durationMs;

  if (resolvedElapsed < 0 || resolvedElapsed > durationMs) {
    updateProgress(Math.max(0, resolvedElapsed), displayDurationMs);
    return;
  }

  // Drift correction only — audio is already playing
  const playerPosMs = audio.currentTime * 1000;
  const diffMs = resolvedElapsed - playerPosMs;
  const absDiff = Math.abs(diffMs);
  if (absDiff > DRIFT_SOFT_LIMIT_MS && diffMs > 0) {
    audio.currentTime = resolvedElapsed / 1000;
    audio.playbackRate = 1.0;
  } else if (diffMs > DRIFT_THRESHOLD_MS) {
    audio.playbackRate = 1.05;
  } else if (diffMs < -DRIFT_THRESHOLD_MS) {
    audio.playbackRate = 0.95;
  } else {
    audio.playbackRate = 1.0;
  }
}

// ── Real-time progress ticker (rAF loop) ─────────────────────────────────────
let _progressRafId = null;
let _progressState = null; // { startedAtMs, durationMs, displayDurationMs }

function startProgressTicker(state) {
  if (_progressRafId) cancelAnimationFrame(_progressRafId);
  const startedAtMs = state?.started_at_ms || Date.now();
  const durationMs = state?.transition_at_ms - startedAtMs;
  const displayDurationMs = (state?.meta?.duration_seconds || state?.duration_seconds)
    ? (state?.meta?.duration_seconds || state?.duration_seconds) * 1000
    : durationMs;
  _progressState = { startedAtMs, durationMs, displayDurationMs };

  function tick() {
    if (!_progressState) return;
    const elapsed = Date.now() - _progressState.startedAtMs;
    updateProgress(Math.max(0, elapsed), _progressState.displayDurationMs);
    if (elapsed < _progressState.durationMs + 2000) {
      _progressRafId = requestAnimationFrame(tick);
    }
  }
  _progressRafId = requestAnimationFrame(tick);
}

function stopProgressTicker() {
  if (_progressRafId) cancelAnimationFrame(_progressRafId);
  _progressRafId = null;
  _progressState = null;
}

function startDriftCorrection() {
  if (driftInterval) clearInterval(driftInterval);
  driftInterval = setInterval(syncToRadio, DRIFT_CHECK_MS);
}

function stopDriftCorrection() {
  if (driftInterval) clearInterval(driftInterval);
  driftInterval = null;
}

function updateProgress(elapsedMs, durationMs) {
  const pct = Math.min(100, (elapsedMs / durationMs) * 100);
  elProgressFill.style.width = `${pct}%`;
  elCurrentTime.textContent = formatTime(elapsedMs / 1000);
  elTotalTime.textContent = formatTime(durationMs / 1000);
}

function updateUI(state) {
  // state bisa berupa full RadioStateResponse (REST/WS RADIO_STATE)
  // atau TrackMeta object langsung — handle keduanya
  const meta = state.meta || state.current_track_meta || (state.title ? state : {});
  const title = meta.title || 'Unknown';
  const artist = meta.artist || 'Unknown';
  elTrackTitle.textContent = title;
  elTrackArtist.textContent = artist;

  // Dynamic page title
  document.title = `${title} — ${artist} | Radiotify 📻`;

  const trackId = state.track_id || state.current_track_id;
  const thumb = meta.thumbnail || meta.thumbnail_url
    || (trackId ? `https://img.youtube.com/vi/${trackId}/maxresdefault.jpg` : null);

  if (thumb) {
    elAlbumArt.innerHTML = `<img src="${thumb}" alt="${title}">`;
    elHeroBg.style.background = `linear-gradient(180deg, rgba(0,0,0,0.3), var(--bg-primary)), url(${thumb}) center/cover`;
  }

  // Dedication / request attribution chip
  const chip = $('dedication-chip');
  if (chip) {
    if (meta.dedication && meta.requested_by) {
      chip.textContent = `💬 "${meta.dedication}" — ${meta.requested_by}`;
      chip.classList.remove('hidden');
    } else if (meta.requested_by) {
      chip.textContent = `Request dari ${meta.requested_by}`;
      chip.classList.remove('hidden');
    } else {
      chip.classList.add('hidden');
    }
  }

  // Mood badge
  const mood = state.mood || radioState?.mood;
  const moodEl = $('mood-badge');
  if (moodEl && mood) {
    moodEl.textContent = `${state.mood_emoji || radioState?.mood_emoji || ''} ${mood}`.trim();
    moodEl.classList.remove('hidden');
  }

  // Media Session API — lock screen / background playback
  setupMediaSession(title, artist, thumb);

  // Update lyrics overlay if open
  if (lyricsActive) {
    const lyricsTitle = $('lyrics-track-title');
    const lyricsArtist = $('lyrics-track-artist');
    const lyricsArt = $('lyrics-album-art');
    if (lyricsTitle) lyricsTitle.textContent = title;
    if (lyricsArtist) lyricsArtist.textContent = artist;
    if (lyricsArt && thumb) lyricsArt.innerHTML = `<img src="${thumb}" alt="">`;
  }
}

// ── Lyrics ────────────────────────────────────────────────

async function fetchLyrics(videoId) {
  if (!videoId) return null;
  try {
    const res = await fetch(`${API_BASE}/radio/lyrics/${videoId}`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.lyrics || null;
  } catch (e) {
    console.error('Failed to fetch lyrics:', e);
    return null;
  }
}

function renderLyrics(lyrics) {
  const container = $('lyrics-content');
  if (!container) return;

  if (!lyrics || !lyrics.length) {
    container.innerHTML = `
      <div class="lyrics-empty">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
        <p>No lyrics available</p>
      </div>
    `;
    const btn = $('btn-lyrics');
    if (btn) btn.classList.add('hidden');
    return;
  }

  // Show lyrics button
  const btn = $('btn-lyrics');
  if (btn) btn.classList.remove('hidden');

  // Build lyrics HTML
  let html = '<div class="lyrics-line-spacer"></div>';
  lyrics.forEach((line, i) => {
    html += `<div class="lyrics-line" data-index="${i}" data-time="${line.time_ms}">${escapeHtml(line.text)}</div>`;
  });
  html += '<div class="lyrics-line-spacer"></div>';
  container.innerHTML = html;
}

function startLyricsSync() {
  if (lyricsRafId) cancelAnimationFrame(lyricsRafId);

  function tick() {
    if (!lyricsActive || !lyricsData || !radioState) return;

    const nowMs = Date.now();
    const startedAt = radioState.started_at_ms || nowMs;
    const elapsed = nowMs - startedAt;

    // Find current line
    let currentIndex = -1;
    for (let i = lyricsData.length - 1; i >= 0; i--) {
      if (elapsed >= lyricsData[i].time_ms) {
        currentIndex = i;
        break;
      }
    }

    // Update line states
    const lines = document.querySelectorAll('.lyrics-line');
    lines.forEach((line, i) => {
      line.classList.remove('active', 'past');
      if (i === currentIndex) {
        line.classList.add('active');
        // Scroll to current line
        if (lyricsActive) {
          const container = $('lyrics-content');
          if (container) {
            const lineRect = line.getBoundingClientRect();
            const containerRect = container.getBoundingClientRect();
            const targetScroll = container.scrollTop + lineRect.top - containerRect.top - (containerRect.height / 3);
            container.scrollTo({ top: targetScroll, behavior: 'smooth' });
          }
        }
      } else if (i < currentIndex) {
        line.classList.add('past');
      }
    });

    lyricsRafId = requestAnimationFrame(tick);
  }

  lyricsRafId = requestAnimationFrame(tick);
}

function stopLyricsSync() {
  if (lyricsRafId) cancelAnimationFrame(lyricsRafId);
  lyricsRafId = null;
}

function openLyricsOverlay() {
  const overlay = $('lyrics-overlay');
  if (!overlay) return;

  // Update track info
  const state = radioState;
  if (state) {
    const meta = state.meta || state.current_track_meta || {};
    const title = meta.title || 'Unknown';
    const artist = meta.artist || 'Unknown';
    const trackId = state.track_id || state.current_track_id;
    const thumb = meta.thumbnail || meta.thumbnail_url
      || (trackId ? `https://img.youtube.com/vi/${trackId}/maxresdefault.jpg` : null);

    const lyricsTitle = $('lyrics-track-title');
    const lyricsArtist = $('lyrics-track-artist');
    const lyricsArt = $('lyrics-album-art');
    if (lyricsTitle) lyricsTitle.textContent = title;
    if (lyricsArtist) lyricsArtist.textContent = artist;
    if (lyricsArt && thumb) lyricsArt.innerHTML = `<img src="${thumb}" alt="">`;
  }

  overlay.classList.remove('hidden');
  lyricsActive = true;
  startLyricsSync();
  requestWakeLock();

  // Mark button as active
  const btn = $('btn-lyrics');
  if (btn) btn.classList.add('active');
}

function closeLyricsOverlay() {
  const overlay = $('lyrics-overlay');
  if (!overlay) return;

  overlay.classList.add('hidden');
  lyricsActive = false;
  stopLyricsSync();
  releaseWakeLock();
  exitTvMode();

  // Remove button active state
  const btn = $('btn-lyrics');
  if (btn) btn.classList.remove('active');
}

// ── TV / Karaoke Mode ───────────────────────────────────────

let tvClockInterval = null;

function updateTvClock() {
  const el = $('tv-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' });
}

function enterTvMode() {
  openLyricsOverlay();
  $('lyrics-overlay')?.classList.add('tv-mode');
  $('tv-clock')?.classList.remove('hidden');
  updateTvClock();
  tvClockInterval = setInterval(updateTvClock, 15000);
  document.documentElement.requestFullscreen?.().catch(() => {});
}

function exitTvMode() {
  const overlay = $('lyrics-overlay');
  if (!overlay || !overlay.classList.contains('tv-mode')) return;
  overlay.classList.remove('tv-mode');
  $('tv-clock')?.classList.add('hidden');
  if (tvClockInterval) { clearInterval(tvClockInterval); tvClockInterval = null; }
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
}

async function requestWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; });
  } catch (e) {
    console.warn('Wake Lock request failed:', e);
  }
}

function releaseWakeLock() {
  if (wakeLock) {
    wakeLock.release();
    wakeLock = null;
  }
}

async function loadLyricsForTrack(videoId) {
  if (!videoId || videoId === currentLyricsVideoId) return;
  currentLyricsVideoId = videoId;

  // Show loading state
  const container = $('lyrics-content');
  if (container && lyricsActive) {
    container.innerHTML = `
      <div class="lyrics-loading">
        <div class="lyrics-loading-spinner"></div>
        <span>Loading lyrics...</span>
      </div>
    `;
  }

  lyricsData = await fetchLyrics(videoId);
  renderLyrics(lyricsData);

  if (lyricsActive && lyricsData) {
    startLyricsSync();
  }
}

// ── WebSocket ──────────────────────────────────────────────

function connectWS() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws`);
  ws.onopen = () => {};
  ws.onclose = () => { setTimeout(connectWS, 3000); };
  ws.onerror = (e) => console.error('WebSocket error:', e);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    handleWSEvent(msg.event, msg.data);
  };
}

async function handleWSEvent(event, data) {
  switch (event) {
    case 'RADIO_STATE':
    case 'TRACK_CHANGED': {
      // Skip playback while maintenance is active
      if (maintenanceActive) break;
      const incomingTrackId = data?.track_id || data?.current_track_id;
      const prevTrackId = radioState?.track_id || radioState?.current_track_id;
      radioState = data;
      // RADIO_STATE uses current_track_meta, TRACK_CHANGED uses meta
      updateUI(data.meta || data.current_track_meta || data);
      if (data?.listeners !== undefined) elListenerNum.textContent = data.listeners;
      // Restart progress ticker for new track
      startProgressTicker(data);
      // Reset HLS so new track's playlist is loaded
      hlsStreamLoaded = false;
      // Compute elapsed from server timestamps so seek is accurate on reconnect
      const nowMs = Date.now();
      const trackChangedElapsed = data?.started_at_ms
        ? Math.max(0, nowMs - data.started_at_ms)
        : 0;
      // Load lyrics for new track
      const newTrackId = data?.track_id || data?.current_track_id;
      if (newTrackId && newTrackId !== prevTrackId) {
        loadLyricsForTrack(newTrackId);
      }
      await playState(data, trackChangedElapsed);
      break;
    }
    case 'NEXT_TRACK':
      break;
      // Preload next track's HLS playlist in background so TRACK_CHANGED has no gap
      if (data.track_id) {
        fetch(`${API_BASE}/live/hls/${data.track_id}/playlist.m3u8`).catch(() => {});
      }
      break;
    case 'QUEUE_UPDATED':
      loadQueue();
      break;
    case 'LISTENER_COUNT':
      if (data?.count !== undefined) elListenerNum.textContent = data.count;
      break;
    case 'SKIP_VOTES':
      updateSkipVoteUI(data.votes || 0, data.needed || 2);
      break;
    case 'MAINTENANCE':
      updateMaintenanceUI(data.enabled, data.message);
      break;
    case 'PLAYBACK_RESUMED':
      // Another device confirmed playback started — hide overlay if still showing
      if (!maintenanceActive) fadeOutOverlay();
      break;
    case 'REACTION':
      if (data?.emoji) spawnReaction(data.emoji);
      break;
  }
}

// ── Reactions ──────────────────────────────────────────────

const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function sendReaction(emoji) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'REACTION', emoji }));
  }
}

function spawnReaction(emoji) {
  if (REDUCED_MOTION) return;
  const layer = $('reaction-layer');
  if (!layer) return;
  const el = document.createElement('span');
  el.className = 'reaction-float';
  el.textContent = emoji;
  el.style.left = `${10 + Math.random() * 80}%`;
  el.style.setProperty('--drift', `${(Math.random() - 0.5) * 80}px`);
  layer.appendChild(el);
  el.addEventListener('animationend', () => el.remove());
}

document.querySelectorAll('.reaction-btn').forEach(btn => {
  btn.addEventListener('click', () => sendReaction(btn.dataset.emoji));
});

// ── Tab Navigation ─────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    $(`tab-${btn.dataset.tab}`).classList.add('active');
    if (btn.dataset.tab === 'history') loadHistory();
    if (btn.dataset.tab === 'search') elSearchInput.focus();
    if (btn.dataset.tab === 'stats') loadStats();
  });
});

// ── Queue UI ───────────────────────────────────────────────

async function loadQueue() {
  try {
    const res = await fetch(`${API_BASE}/queue`);
    const data = await res.json();
    renderQueue(data.queue || []);
  } catch (e) { console.error('Failed to load queue:', e); }
}

function renderQueue(queue) {
  if (!queue.length) {
    elQueueList.innerHTML = '<div class="empty-state">No songs in queue — search and add one!</div>';
    return;
  }
  elQueueList.innerHTML = queue.map((item, i) => `
    <div class="queue-item" data-index="${i}">
      <div class="queue-item-pos">${i + 1}</div>
      <div class="queue-item-thumb">
        ${item.thumbnail ? `<img src="${item.thumbnail}" alt="">` : ''}
      </div>
      <div class="queue-item-info">
        <div class="queue-item-title">${escapeHtml(item.title || 'Unknown')}</div>
        <div class="queue-item-artist">${escapeHtml(item.artist || 'Unknown')}${item.source === 'user' && item.added_by ? ` · oleh ${escapeHtml(item.added_by)}` : ''}</div>
      </div>
      ${isAdmin ? `<button class="queue-item-remove" data-index="${i}" title="Remove">✕</button>` : ''}
    </div>
  `).join('');

  // Remove buttons
  elQueueList.querySelectorAll('.queue-item-remove').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.index);
      if (confirm('Remove this song from queue?')) {
        try {
          await fetch(`${API_BASE}/admin/queue/${idx}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${adminToken}` },
          });
          loadQueue();
        } catch { console.error('Failed to remove'); }
      }
    });
  });
}

// ── Search ─────────────────────────────────────────────────

let searchTimeout = null;
let searchErrorTimeout = null;

function showSearchError(message) {
  clearTimeout(searchErrorTimeout);
  let errorEl = $('search-error');
  if (!errorEl) {
    errorEl = document.createElement('div');
    errorEl.id = 'search-error';
    errorEl.className = 'search-error';
    elSearchInput.closest('.search-bar').after(errorEl);
  }
  errorEl.textContent = message;
  errorEl.classList.add('active');
  searchErrorTimeout = setTimeout(() => errorEl.classList.remove('active'), 4200);
}

async function doSearch(query) {
  if (query.length < 3) { elSearchResults.innerHTML = ''; return; }
  elSearchResults.innerHTML = '<div class="search-loading">Searching...</div>';
  try {
    const res = await fetch(`${API_BASE}/queue/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    renderSearchResults(data.results || []);
  } catch { elSearchResults.innerHTML = '<div class="search-loading">Search failed</div>'; }
}

function renderSearchResults(results) {
  if (!results.length) { elSearchResults.innerHTML = '<div class="search-loading">No results</div>'; return; }
  elSearchResults.innerHTML = results.map(r => `
    <div class="search-result-item" data-video-id="${r.video_id}" data-thumb="${r.thumbnail || ''}">
      <div class="search-result-thumb">
        ${r.thumbnail ? `<img src="${r.thumbnail}" alt="">` : ''}
      </div>
      <div class="search-result-info">
        <div class="search-result-title">${escapeHtml(r.title)}</div>
        <div class="search-result-artist">${escapeHtml(r.artist || '')}</div>
      </div>
    </div>
  `).join('');

  elSearchResults.querySelectorAll('.search-result-item').forEach(el => {
    el.addEventListener('click', () => {
      if (el.classList.contains('is-adding')) return;
      openRequestModal(el);
    });
  });
}

// ── Request flow (nickname + optional dedication) ──────────

let pendingRequestEl = null;

function getNickname() { return localStorage.getItem('nickname') || ''; }
function setNickname(name) { localStorage.setItem('nickname', name); }

function openRequestModal(el) {
  pendingRequestEl = el;
  const title = el.querySelector('.search-result-title').textContent;
  const artist = el.querySelector('.search-result-artist').textContent;
  $('request-track-label').textContent = `${title} — ${artist}`;
  $('request-nickname').value = getNickname();
  $('request-message').value = '';
  $('request-modal').classList.add('active');
  ($('request-nickname').value ? $('request-message') : $('request-nickname')).focus();
}

function closeRequestModal() {
  $('request-modal').classList.remove('active');
  pendingRequestEl = null;
}

async function submitRequest() {
  const el = pendingRequestEl;
  if (!el) return;
  const videoId = el.dataset.videoId;
  const titleEl = el.querySelector('.search-result-title');
  const artistEl = el.querySelector('.search-result-artist');
  const title = titleEl.textContent;
  const artist = artistEl.textContent;
  const nickname = $('request-nickname').value.trim();
  const message = $('request-message').value.trim();
  if (nickname) setNickname(nickname);

  closeRequestModal();
  el.classList.add('is-adding');
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (adminToken) headers['Authorization'] = `Bearer ${adminToken}`;
    const res = await fetch(`${API_BASE}/queue`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ video_id: videoId, title, artist, nickname, message }),
    });
    if (!res.ok) {
      let detail = '';
      try {
        const data = await res.json();
        detail = data.detail || data.message || '';
      } catch {}
      if (res.status === 429) {
        throw new Error(detail || 'Kebanyakan request. Tunggu sebentar sebelum add lagu lagi.');
      }
      throw new Error(detail || 'Gagal add lagu ke queue.');
    }

    titleEl.classList.remove('search-added');
    void titleEl.offsetWidth;
    titleEl.classList.add('search-added');
    titleEl.textContent = '✓ Added!';
    artistEl.textContent = 'Added to queue';
    loadQueue();
    setTimeout(() => {
      titleEl.classList.remove('search-added');
      titleEl.textContent = title;
      artistEl.textContent = artist;
      el.classList.remove('is-adding');
    }, 1200);
  } catch (e) {
    console.error('Failed to add to queue:', e);
    showSearchError(e.message || 'Gagal add lagu. Coba lagi sebentar.');
    el.classList.remove('is-adding');
  }
}

$('btn-send-request')?.addEventListener('click', submitRequest);
$('request-modal-close')?.addEventListener('click', closeRequestModal);
$('request-message')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitRequest(); });

elSearchInput.addEventListener('input', (e) => {
  const query = e.target.value.trim();
  clearTimeout(searchTimeout);
  // Clear results immediately if query too short
  if (query.length < 3) {
    elSearchResults.innerHTML = '';
    return;
  }
  // Show skeleton while waiting
  elSearchResults.innerHTML = `
    <div class="search-skeleton">
      ${Array(4).fill('<div class="skeleton-item"><div class="skeleton-thumb"></div><div class="skeleton-lines"><div class="skeleton-line skeleton-line--title"></div><div class="skeleton-line skeleton-line--sub"></div></div></div>').join('')}
    </div>`;
  searchTimeout = setTimeout(() => doSearch(query), 3000);
});

// ── History (public) ───────────────────────────────────────

async function loadHistory() {
  loadTopTracks();
  try {
    const res = await fetch(`${API_BASE}/radio/history?limit=20`);
    if (!res.ok) { elHistoryList.innerHTML = '<div class="empty-state">Failed to load history</div>'; return; }
    const data = await res.json();
    renderHistory(data.history || []);
  } catch { elHistoryList.innerHTML = '<div class="empty-state">Failed to load history</div>'; }
}

async function loadTopTracks() {
  const container = $('top-tracks');
  if (!container) return;
  try {
    const res = await fetch(`${API_BASE}/radio/top?days=7&limit=5`);
    if (!res.ok) { container.innerHTML = ''; return; }
    const data = await res.json();
    const top = data.top || [];
    if (!top.length) { container.innerHTML = ''; return; }
    container.innerHTML = `
      <div class="top-tracks-title">Top minggu ini</div>
      ${top.map((t, i) => `
        <div class="top-track-item">
          <div class="top-track-rank">${i + 1}</div>
          <div class="top-track-thumb">${t.thumbnail_url ? `<img src="${t.thumbnail_url}" alt="" loading="lazy">` : ''}</div>
          <div class="top-track-info">
            <div class="top-track-name">${escapeHtml(t.title || t.video_id)}</div>
            <div class="top-track-artist">${escapeHtml(t.artist || '')}</div>
          </div>
          <div class="top-track-count">${t.play_count}×</div>
        </div>
      `).join('')}
      <div class="top-tracks-title" style="margin-top: 18px;">Baru diputar</div>
    `;
  } catch { container.innerHTML = ''; }
}

function renderHistory(items) {
  if (!items.length) { elHistoryList.innerHTML = '<div class="empty-state">No history yet</div>'; return; }
  elHistoryList.innerHTML = items.map(h => {
    const thumb = h.thumbnail || h.thumbnail_url || null;
    const thumbHtml = thumb
      ? `<img src="${thumb}" alt="" loading="lazy">`
      : `<svg viewBox="0 0 24 24" fill="currentColor" width="40" height="40"><path d="M12 3v9.28a4.39 4.39 0 0 0-1.5-.28C8.01 12 6 14.01 6 16.5S8.01 21 10.5 21c2.31 0 4.2-1.75 4.45-4H15V6h4V3h-7z"/></svg>`;
    const time = h.played_at ? new Date(h.played_at * 1000).toLocaleString() : '';
    return `
      <div class="history-item">
        <div class="history-item-thumb">${thumbHtml}</div>
        <div class="history-item-info">
          <div class="history-item-title">${escapeHtml(h.title || h.video_id)}</div>
          <div class="history-item-meta">${escapeHtml(h.artist || '')} ${time ? '• ' + time : ''}</div>
        </div>
      </div>
    `;
  }).join('');
}

// ── Admin ──────────────────────────────────────────────────

async function adminLogin(password) {
  try {
    const res = await fetch(`${API_BASE}/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) throw new Error('Invalid');
    const data = await res.json();
    adminToken = data.token;
    localStorage.setItem('admin_token', adminToken);
    isAdmin = true;
    showAdminUI();
    $('admin-login-modal').classList.remove('active');
    $('admin-error').classList.add('hidden');
    loadHistory();
    syncMaintenanceState(); // sync maintenance state after login
  } catch {
    $('admin-error').classList.remove('hidden');
  }
}

function showAdminUI() {
  elAdminBadge.classList.remove('hidden');
  elAdminBar.classList.remove('hidden');
  elQueueAdminActions.classList.remove('hidden');
  $('tab-btn-stats').classList.remove('hidden');
  // Show disable button on maintenance overlay if it's visible
  if (!$('maintenance-overlay').classList.contains('hidden')) {
    $('maintenance-btn-disable').classList.remove('hidden');
  }
  loadQueue(); // re-render with remove buttons
  refreshMediaSession();
}

function adminLogout() {
  adminToken = null;
  isAdmin = false;
  localStorage.removeItem('admin_token');
  elAdminBadge.classList.add('hidden');
  elAdminBar.classList.add('hidden');
  elQueueAdminActions.classList.add('hidden');
  $('tab-btn-stats').classList.add('hidden');
  // Switch away from stats/history if active
  if (document.querySelector('[data-tab="stats"].active') || document.querySelector('[data-tab="history"].active')) {
    document.querySelector('[data-tab="queue"]').click();
  }
  loadQueue();
  refreshMediaSession();
}

async function adminFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    headers: { ...options.headers, 'Authorization': `Bearer ${adminToken}` },
  });
}

// Admin event listeners
$('btn-admin-login').addEventListener('click', () => adminLogin($('admin-password').value));
$('admin-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') adminLogin(e.target.value); });
$('admin-login-close').addEventListener('click', () => $('admin-login-modal').classList.remove('active'));
$('btn-admin-logout').addEventListener('click', adminLogout);

// Lyrics event listeners
$('btn-lyrics')?.addEventListener('click', openLyricsOverlay);
$('btn-lyrics-close')?.addEventListener('click', closeLyricsOverlay);

// ── Maintenance Mode ──────────────────────────────────────

let maintenanceActive = false;

async function syncMaintenanceState() {
  try {
    const res = await adminFetch(`${API_BASE}/admin/maintenance`);
    if (!res) return;
    const data = await res.json();
    maintenanceActive = data.enabled;
    updateMaintenanceUI(data.enabled, data.message);
  } catch (_) {}
}

function updateMaintenanceUI(enabled, message) {
  const label = $('maintenance-btn-label');
  const btn = $('btn-maintenance');
  const overlay = $('maintenance-overlay');

  // Set synchronously — WS handlers that fire right after see correct state
  maintenanceActive = enabled;
  label.textContent = enabled ? 'Disable Maintenance' : 'Maintenance';
  btn.classList.toggle('active-maintenance', enabled);

  if (enabled) {
    // ── Maintenance ON ──────────────────────────────────────
    maintenanceStartedAt = Date.now(); // record when maintenance started
    stopDriftCorrection();
    stopProgressTicker();
    if (hlsPlayer) {
      hlsPlayer.stopLoad();
      hlsPlayer.destroy();
      hlsPlayer = null;
      hlsStreamLoaded = false;
    }
    getActiveAudio()?.pause();
    getInactiveAudio()?.pause();
    // Keep WS alive — device needs it to receive MAINTENANCE off broadcast
    setPlaybackStatus('Maintenance mode', 'err');
    overlay.classList.remove('hidden');
    if (isAdmin) $('maintenance-btn-disable').classList.remove('hidden');
    const msgEl = $('maintenance-message');
    if (msgEl && message) msgEl.textContent = message;

  } else {
    // ── Maintenance OFF ─────────────────────────────────────
    $('maintenance-btn-disable').classList.add('hidden');
    hlsStreamLoaded = false;
    if (!ws) connectWS();
    if (!driftInterval) startDriftCorrection();

    // Fetch fresh state and start playback immediately.
    // Don't rely on TRACK_CHANGED — it may have already been sent before
    // this device processed the MAINTENANCE off event.
    fetch(`${API_BASE}/radio/state`)
      .then(r => r.ok ? r.json() : null)
      .then(async state => {
        if (!state || maintenanceActive) return;
        if (state.track_id || state.current_track_id) {
          radioState = state;
          // started_at_ms already corrected by backend to reflect paused position
          const elapsedMs = state.started_at_ms
            ? Math.max(0, Date.now() - state.started_at_ms)
            : 0;
          maintenanceStartedAt = null;
          updateUI(state);
          updatePlaybackModeLabel();
          startProgressTicker(state);
          await playState(state, elapsedMs);
        }
        // Hide overlay once audio is actually playing, fallback 4s
        const audio = getActiveAudio();
        await new Promise(resolve => {
          if (!audio || !audio.paused) { resolve(); return; }
          const t = setTimeout(resolve, 4000);
          audio.addEventListener('playing', () => { clearTimeout(t); resolve(); }, { once: true });
        });
        if (!maintenanceActive) {
          // Notify backend — triggers PLAYBACK_RESUMED broadcast to all other devices
          fetch(`${API_BASE}/radio/playback-resumed`, { method: 'POST' }).catch(() => {});
          fadeOutOverlay();
        }
      })
      .catch(() => { if (!maintenanceActive) fadeOutOverlay(); });
  }
}

function fadeOutOverlay() {
  const overlay = $('maintenance-overlay');
  overlay.style.transition = 'opacity 0.4s ease';
  overlay.style.opacity = '0';
  setTimeout(() => {
    overlay.classList.add('hidden');
    overlay.style.opacity = '';
    overlay.style.transition = '';
  }, 400);
}

// Maintenance overlay buttons
$('maintenance-btn-settings').addEventListener('click', () => {
  if (isAdmin) {
    // Already admin — just show the disable button
    $('maintenance-btn-disable').classList.remove('hidden');
  } else {
    $('admin-login-modal').classList.add('active');
    $('admin-password').focus();
  }
});

$('maintenance-btn-disable').addEventListener('click', async () => {
  const res = await adminFetch(`${API_BASE}/admin/maintenance?enabled=false`, { method: 'POST' });
  if (res?.ok) {
    const data = await res.json();
    updateMaintenanceUI(data.enabled, data.message);
  } else {
    alert(`Failed to disable maintenance (${res?.status})`);
  }
});

$('btn-maintenance').addEventListener('click', async () => {
  const newState = !maintenanceActive;
  const params = new URLSearchParams({ enabled: newState });
  const res = await adminFetch(`${API_BASE}/admin/maintenance?${params}`, { method: 'POST' });
  if (res?.ok) {
    const data = await res.json();
    updateMaintenanceUI(data.enabled);
  } else {
    alert(`Failed to toggle maintenance mode (${res?.status})`);
  }
});

$('btn-skip').addEventListener('click', async () => {
  await adminFetch(`${API_BASE}/admin/skip`, { method: 'POST' });
});



$('btn-clear-queue').addEventListener('click', async () => {
  if (!confirm('Clear entire queue?')) return;
  await adminFetch(`${API_BASE}/admin/queue`, { method: 'DELETE' });
  loadQueue();
});

$('btn-lock-queue').addEventListener('click', async () => {
  const btn = $('btn-lock-queue');
  const isLocked = $('lock-queue-label').textContent.includes('Unlock');
  const endpoint = isLocked ? 'unlock' : 'lock';
  await adminFetch(`${API_BASE}/admin/queue/${endpoint}`, { method: 'POST' });
  const svgLock = `<path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/>`;
  const svgUnlock = `<path d="M12 1C9.24 1 7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2h-1V6c0-2.76-2.24-5-5-5zm0 2c1.66 0 3 1.34 3 3v2H9V6c0-1.66 1.34-3 3-3zm0 11c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/>`;
  $('lock-queue-icon').innerHTML = isLocked ? svgLock : svgUnlock;
  $('lock-queue-label').textContent = isLocked ? 'Lock Queue' : 'Unlock Queue';
});

// ── Settings Menu ──────────────────────────────────────────

$('fab-settings').addEventListener('click', () => {
  $('settings-menu').classList.toggle('hidden');
});
document.addEventListener('click', (e) => {
  if (!$('settings-menu').contains(e.target) && e.target !== $('fab-settings')) {
    $('settings-menu').classList.add('hidden');
  }
});
$('settings-admin').addEventListener('click', () => {
  $('settings-menu').classList.add('hidden');
  if (isAdmin) {
    alert('Already logged in as admin');
  } else {
    $('admin-login-modal').classList.add('active');
    $('admin-password').focus();
  }
});
function updatePlaybackModeLabel() {
  playbackMode = 'audio';
  localStorage.setItem('playback_mode', playbackMode);
  $('settings-playback-label').textContent = 'Mode: HLS Audio Stream';
}

$('settings-install').addEventListener('click', async () => {
  $('settings-menu').classList.add('hidden');

  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice.catch(() => null);
    deferredInstallPrompt = null;
    updateInstallButton();
    return;
  }

  if (isIosDevice) {
    alert('Install Radiotify:\n\n1. Tap tombol Share di Safari\n2. Pilih Add to Home Screen\n3. Tap Add');
    return;
  }

  alert('Install belum tersedia di browser ini. Pastikan buka via HTTPS, lalu coba refresh.');
});

$('settings-playback').addEventListener('click', async () => {
  playbackMode = 'audio';
  localStorage.setItem('playback_mode', playbackMode);
  updatePlaybackModeLabel();
  $('settings-menu').classList.add('hidden');
  await syncToRadio();
});

$('settings-about').addEventListener('click', () => {
  $('settings-menu').classList.add('hidden');
  alert('📻 Radiotify — Synchronized Web Radio\n\nReal-time synchronized radio (Audio Stream mode).\nAll listeners hear the same playback position.');
});
updatePlaybackModeLabel();

// ── Nickname setting ────────────────────────────────────────

function updateNicknameLabel() {
  const label = $('settings-nickname-label');
  if (label) label.textContent = getNickname() ? `Nickname: ${getNickname()}` : 'Set Nickname';
}

$('settings-nickname')?.addEventListener('click', () => {
  $('settings-menu').classList.add('hidden');
  const name = prompt('Nickname kamu (dipakai saat request lagu):', getNickname());
  if (name !== null) {
    setNickname(name.trim().slice(0, 24));
    updateNicknameLabel();
  }
});
updateNicknameLabel();

// ── TV Mode setting ─────────────────────────────────────────

$('settings-tv')?.addEventListener('click', () => {
  $('settings-menu').classList.add('hidden');
  enterTvMode();
});

// ── Share ───────────────────────────────────────────────────

function showToast(text) {
  const toast = $('toast');
  if (!toast) return;
  toast.textContent = text;
  toast.classList.remove('hidden');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.add('hidden'), 2400);
}

$('btn-share')?.addEventListener('click', async () => {
  const url = `${location.origin}/share`;
  const meta = radioState?.meta || radioState?.current_track_meta || {};
  const text = meta.title ? `Lagi dengerin ${meta.title} — ${meta.artist} di Radiotify` : 'Dengar bareng di Radiotify';
  if (navigator.share) {
    try { await navigator.share({ title: 'Radiotify', text, url }); } catch {}
  } else {
    try {
      await navigator.clipboard.writeText(url);
      showToast('Link disalin!');
    } catch { showToast('Gagal menyalin link'); }
  }
});

// ── Vote Skip ───────────────────────────────────────────────

async function voteSkip() {
  try {
    const res = await fetch(`${API_BASE}/radio/vote-skip`, { method: 'POST' });
    const data = await res.json();
    if (data.skipped) {
      updateSkipVoteUI(0, 0);
    } else {
      updateSkipVoteUI(data.votes, data.needed);
    }
  } catch (e) { console.error('Vote skip failed:', e); }
}

function updateSkipVoteUI(votes, needed) {
  const bar = $('skip-vote-bar');
  const btn = $('btn-vote-skip');
  if (!bar || !btn) return;
  if (votes === 0 && needed === 0) {
    bar.style.display = 'none';
    return;
  }
  bar.style.display = 'flex';
  const pct = Math.min(100, (votes / needed) * 100);
  $('skip-vote-fill').style.width = `${pct}%`;
  $('skip-vote-text').textContent = `${votes}/${needed} votes to skip`;
}

$('btn-vote-skip').addEventListener('click', voteSkip);

// ── Media Session API ──────────────────────────────────────

function refreshMediaSession() {
  if (!radioState) return;
  const meta = radioState.meta || radioState.current_track_meta || {};
  const trackId = radioState.track_id || radioState.current_track_id;
  const thumb = meta.thumbnail || meta.thumbnail_url
    || (trackId ? `https://img.youtube.com/vi/${trackId}/maxresdefault.jpg` : null);
  setupMediaSession(meta.title, meta.artist, thumb);
}

function setupMediaSession(title, artist, thumbnail) {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: title || 'Radiotify',
    artist: artist || 'Synced Radio',
    artwork: thumbnail ? [{ src: thumbnail, sizes: '512x512', type: 'image/jpeg' }] : [],
  });
  navigator.mediaSession.playbackState = 'playing';
  navigator.mediaSession.setActionHandler('play', () => {
    if (isNativeAudioMode()) getActiveAudio()?.play();
    else getActivePlayer()?.playVideo?.();
  });
  navigator.mediaSession.setActionHandler('pause', () => {
    if (isNativeAudioMode()) getActiveAudio()?.pause();
    else getActivePlayer()?.pauseVideo?.();
  });
  // Always set nexttrack — admin uses admin-skip, non-admin uses vote-skip
  navigator.mediaSession.setActionHandler('nexttrack', async () => {
    try {
      if (isAdmin) {
        await adminFetch(`${API_BASE}/admin/skip`, { method: 'POST' });
      } else {
        await fetch(`${API_BASE}/radio/vote-skip`, { method: 'POST' });
      }
    } catch {}
  });
}

// ── Stats ──────────────────────────────────────────────────

async function loadStats() {
  try {
    const res = await fetch(`${API_BASE}/radio/stats`);
    const s = await res.json();
    renderStats(s);
  } catch { $('stats-content').innerHTML = '<div class="empty-state">Failed to load stats</div>'; }
}

function renderStats(s) {
  const uptime = s.uptime_seconds ? formatUptime(s.uptime_seconds) : 'N/A';
  const topHtml = (s.top_tracks || []).map(t =>
    `<div class="stats-track">🎵 ${escapeHtml(t.title)} — ${escapeHtml(t.artist || '?')} <span class="stats-count">(${t.cnt}x)</span></div>`
  ).join('') || '<div class="empty-state">No data yet</div>';
  const srcHtml = Object.entries(s.sources || {}).map(([k, v]) =>
    `<span class="stats-badge">${k}: ${v}</span>`
  ).join(' ') || '<span class="stats-badge">auto: 0</span>';

  $('stats-content').innerHTML = `
    <div class="stats-grid">
      <div class="stats-card"><div class="stats-value">${s.total_plays || 0}</div><div class="stats-label">Total Plays</div></div>
      <div class="stats-card"><div class="stats-value">${s.plays_24h || 0}</div><div class="stats-label">Plays (24h)</div></div>
      <div class="stats-card"><div class="stats-value">${s.listeners || 0}</div><div class="stats-label">Listeners</div></div>
      <div class="stats-card"><div class="stats-value">${s.queue_length || 0}</div><div class="stats-label">Queue</div></div>
    </div>
    <div class="stats-section">
      <div class="stats-section-title">⏱ Uptime</div>
      <div>${uptime}</div>
    </div>
    <div class="stats-section">
      <div class="stats-section-title">🔥 Top Tracks (7d)</div>
      ${topHtml}
    </div>
    <div class="stats-section">
      <div class="stats-section-title">📊 Sources (24h)</div>
      ${srcHtml}
    </div>
  `;
}

function formatUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return d > 0 ? `${d}d ${h}h ${m}m` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// ── Helpers ────────────────────────────────────────────────

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ── Init ───────────────────────────────────────────────────

function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || location.protocol !== 'https:') return;
  navigator.serviceWorker.register('/sw.js').catch((err) => {
    console.warn('Service worker registration failed:', err);
  });
}

function updateInstallButton() {
  const btn = $('settings-install');
  if (!btn) return;

  const svgInstall = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>`;
  const svgCheck = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>`;

  if (isStandalonePwa) {
    btn.classList.add('hidden');
    return;
  }

  if (deferredInstallPrompt) {
    btn.innerHTML = `${svgInstall} Install App`;
    btn.disabled = false;
    btn.classList.remove('hidden');
    return;
  }

  if (isIosDevice) {
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M17 1.01L7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-1.99-2-1.99zM17 19H7V5h10v14z"/></svg> Add to Home Screen`;
    btn.disabled = false;
    btn.classList.remove('hidden');
    return;
  }

  btn.innerHTML = `${svgInstall} Install App`;
  btn.disabled = true;
  btn.classList.remove('hidden');
}

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallButton();
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  localStorage.setItem('pwa_installed', '1');
  updateInstallButton();
});

let audioUnlocked = false;
let playersInitialized = false;

async function init() {
  registerServiceWorker();
  updateInstallButton();

  // Init audio players first
  await initPlayers();
  playersInitialized = true;

  // Single fetch — state includes maintenance status
  try {
    const res = await fetch('/api/radio/state');
    if (res.ok) {
      const initialState = await res.json();

      // Handle maintenance
      if (initialState.maintenance) {
        updateMaintenanceUI(true, initialState.maintenance_message);
      }

      // Start playback if track available and not in maintenance
      if (!initialState.maintenance && (initialState.track_id || initialState.current_track_id)) {
        radioState = initialState;
        const elapsedMs = initialState.started_at_ms
          ? Math.max(0, Date.now() - initialState.started_at_ms)
          : 0;
        updateUI(initialState);
        updatePlaybackModeLabel();
        startProgressTicker(initialState);
        await playState(initialState, elapsedMs);
        // Load lyrics for initial track
        const initialTrackId = initialState.track_id || initialState.current_track_id;
        if (initialTrackId) {
          loadLyricsForTrack(initialTrackId);
        }
      }
    }
  } catch (err) {
    console.warn('Initial state fetch failed:', err);
  }

  connectWS();
  await loadQueue();

  // Check stored admin token
  if (adminToken) {
    try {
      const res = await fetch(`${API_BASE}/admin/history?limit=1`, {
        headers: { 'Authorization': `Bearer ${adminToken}` },
      });
      if (res.ok) { isAdmin = true; showAdminUI(); }
      else { adminLogout(); }
    } catch { /* ignore */ }
  }

  startDriftCorrection();
}

function unlockAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;
  $('tap-overlay').classList.add('hidden');
  init();
}

const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 0);
if (isMobile) {
  $('tap-overlay').addEventListener('click', unlockAudio, { once: true });
  $('tap-overlay').addEventListener('touchend', unlockAudio, { once: true });
} else {
  // Desktop: probe autoplay policy first
  const probeAudio = new Audio();
  probeAudio.volume = 0;
  probeAudio.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
  probeAudio.play().then(() => {
    // Autoplay allowed — proceed without overlay
    probeAudio.pause();
    $('tap-overlay').classList.add('hidden');
    init();
  }).catch(() => {
    // Autoplay blocked — show overlay and wait for click
    $('tap-overlay').addEventListener('click', unlockAudio, { once: true });
  });
}

// Re-acquire wake lock when page becomes visible (browser releases it on background)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && lyricsActive) {
    requestWakeLock();
  }
});
