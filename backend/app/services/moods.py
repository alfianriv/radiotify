"""Time-of-day mood blocks — seed the random-track fallback like a real
radio programming clock. Hours are server-local."""
from datetime import datetime
from typing import Dict, Any, Optional

MOOD_BLOCKS = [
    {'start': 5,  'end': 10, 'name': 'Morning Fresh', 'emoji': '☀️',
     'queries': ['lagu pagi semangat', 'morning acoustic hits', 'feel good pop morning']},
    {'start': 10, 'end': 16, 'name': 'Daytime Hits', 'emoji': '🎧',
     'queries': ['top hits indonesia', 'pop hits 2026', 'trending pop songs']},
    {'start': 16, 'end': 20, 'name': 'Golden Hour', 'emoji': '🌇',
     'queries': ['golden hour chill pop', 'lagu sore santai', 'sunset indie pop']},
    {'start': 20, 'end': 24, 'name': 'Night Vibes', 'emoji': '🌙',
     'queries': ['lagu galau malam', 'late night r&b chill', 'lagu pop sendu']},
    {'start': 0,  'end': 5,  'name': 'Late Night', 'emoji': '🌌',
     'queries': ['sad indie midnight', 'lofi sleep chill', 'slow ballad malam']},
]


def get_mood(hour: Optional[int] = None) -> Dict[str, Any]:
    """Return the mood block for the given hour (default: current local hour)."""
    if hour is None:
        hour = datetime.now().hour
    hour = hour % 24
    for block in MOOD_BLOCKS:
        if block['start'] <= hour < block['end']:
            return block
    return MOOD_BLOCKS[-1]
