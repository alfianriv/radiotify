"""AI DJ — radio-style interstitials between songs.

Script: OpenRouter chat completion (free-tier model by default).
Voice:  edge-tts Indonesian neural voice (free, keyless).
Clips land in the audio cache as dj-<ts>.mp3 so the existing
AudioCache/HLS pipeline serves them like any other track.
Every failure returns None — the engine just transitions normally.
"""
import asyncio
import glob
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from ..config import config

logger = logging.getLogger(__name__)

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'

SYSTEM_PROMPT = (
    "Kamu penyiar radio Radiotify — santai, hangat, bahasa Indonesia sehari-hari "
    "seperti penyiar radio anak muda. Tugasmu: tulis satu naskah siaran singkat "
    "(maksimal 45 kata) untuk jeda antar lagu. Sebut lagu yang barusan diputar dan "
    "lagu berikutnya. Kalau ada request/salam dari pendengar, bacakan salamnya. "
    "Boleh sebut suasana waktu (pagi/sore/malam) atau jumlah pendengar jika menarik. "
    "JANGAN pakai emoji, tanda kutip, markdown, atau sapaan berlebihan. "
    "Langsung tulis naskahnya saja."
)

# Strip anything TTS would read awkwardly: emoji, markdown markers, quotes
_EMOJI_RE = re.compile(
    '[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⭐❤️]+'
)


def sanitize_script(text: str) -> str:
    """Clean LLM output for TTS: no emoji, markdown, or wrapping quotes."""
    text = _EMOJI_RE.sub('', text or '')
    text = re.sub(r'[*_#`~\[\]()"“”\']+', '', text)
    text = ' '.join(text.split()).strip()
    return text


def build_context(prev_meta: Optional[Dict[str, Any]],
                  next_meta: Optional[Dict[str, Any]],
                  mood: str, listeners: int) -> Dict[str, Any]:
    """Compact facts the script prompt is allowed to use."""
    prev_meta = prev_meta or {}
    next_meta = next_meta or {}
    return {
        'prev': f"{prev_meta.get('title', '?')} — {prev_meta.get('artist', '?')}",
        'next': f"{next_meta.get('title', '?')} — {next_meta.get('artist', '?')}",
        'requested_by': next_meta.get('requested_by'),
        'dedication': next_meta.get('dedication'),
        'mood': mood,
        'listeners': listeners,
        'time': datetime.now().strftime('%H:%M'),
    }


def probe_duration(path: str) -> Optional[float]:
    """Clip duration in seconds via ffprobe."""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


class DjService:
    def __init__(self):
        self.cache_dir = Path(config.AUDIO_CACHE_DIR)

    @property
    def enabled(self) -> bool:
        return config.DJ_ENABLED and bool(config.OPENROUTER_API_KEY)

    async def _generate_script(self, ctx: Dict[str, Any]) -> Optional[str]:
        user_parts = [
            f"Barusan diputar: {ctx['prev']}",
            f"Lagu berikutnya: {ctx['next']}",
            f"Suasana: {ctx['mood']}, jam {ctx['time']}, {ctx['listeners']} pendengar",
        ]
        if ctx.get('requested_by'):
            ded = f", pesannya: {ctx['dedication']}" if ctx.get('dedication') else ''
            user_parts.append(f"Lagu berikutnya adalah request dari {ctx['requested_by']}{ded}")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.post(
                    OPENROUTER_URL,
                    headers={
                        'Authorization': f'Bearer {config.OPENROUTER_API_KEY}',
                        'X-Title': 'Radiotify AI DJ',
                    },
                    json={
                        'model': config.DJ_MODEL,
                        'messages': [
                            {'role': 'system', 'content': SYSTEM_PROMPT},
                            {'role': 'user', 'content': '\n'.join(user_parts)},
                        ],
                        'max_tokens': 200,
                        'temperature': 0.9,
                    },
                )
            if res.status_code != 200:
                logger.warning(f"DJ script request failed: HTTP {res.status_code} {res.text[:200]}")
                return None
            script = sanitize_script(res.json()['choices'][0]['message']['content'])
            # Too-short output means the model returned junk
            return script if len(script) >= 20 else None
        except Exception as e:
            logger.warning(f"DJ script generation error: {e}")
            return None

    async def generate_clip(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Script → TTS mp3 → {'clip_id', 'duration_seconds', 'script'} or None."""
        script = await self._generate_script(ctx)
        if not script:
            return None
        clip_id = f"dj-{int(time.time())}"
        path = self.cache_dir / f"{clip_id}.mp3"
        try:
            import edge_tts
            await edge_tts.Communicate(script, voice=config.DJ_VOICE, rate='+8%').save(str(path))
        except Exception as e:
            logger.warning(f"DJ TTS error: {e}")
            return None
        duration = probe_duration(str(path))
        if not duration or duration < 2:
            path.unlink(missing_ok=True)
            return None
        self._cleanup_old(keep=clip_id)
        logger.info(f"🎙 DJ clip ready ({duration:.1f}s): {script[:80]}...")
        return {'clip_id': clip_id, 'duration_seconds': duration, 'script': script}

    def _cleanup_old(self, keep: str) -> None:
        """Keep only the newest clips; DJ clips are throwaway."""
        clips = sorted(glob.glob(str(self.cache_dir / 'dj-*.mp3')), reverse=True)
        for old in clips[2:]:
            if keep not in old:
                try:
                    os.remove(old)
                except OSError:
                    pass
