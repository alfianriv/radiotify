"""Redis state manager for radio runtime state."""
import json
import time
import logging
from typing import Optional, List, Dict, Any

import redis

from ..config import config

logger = logging.getLogger(__name__)


class RedisState:
    def __init__(self):
        self.redis = redis.from_url(config.REDIS_URL, decode_responses=True)

    # ── Radio State ─────────────────────────────────────────

    def get_radio_state(self) -> Optional[Dict[str, Any]]:
        """Get current radio state."""
        data = self.redis.get('radio:state')
        if data:
            return json.loads(data)
        return None

    def set_radio_state(self, state: Dict[str, Any]) -> None:
        """Set radio state."""
        self.redis.set('radio:state', json.dumps(state))

    def clear_radio_state(self) -> None:
        """Clear radio state."""
        self.redis.delete('radio:state')

    # ── Queue ───────────────────────────────────────────────

    def get_queue(self) -> List[Dict[str, Any]]:
        """Get full queue."""
        data = self.redis.get('radio:queue')
        if data:
            return json.loads(data)
        return []

    def set_queue(self, queue: List[Dict[str, Any]]) -> None:
        """Set full queue."""
        self.redis.set('radio:queue', json.dumps(queue))

    def add_to_queue(self, item: Dict[str, Any]) -> bool:
        """Add item to end of queue. Returns False if duplicate."""
        queue = self.get_queue()
        video_id = item.get('video_id')
        # Deduplicate
        if any(q.get('video_id') == video_id for q in queue):
            return False
        queue.append(item)
        self.set_queue(queue)
        return True

    def remove_from_queue(self, index: int) -> Optional[Dict[str, Any]]:
        """Remove item at index. Returns removed item or None."""
        queue = self.get_queue()
        if 0 <= index < len(queue):
            removed = queue.pop(index)
            self.set_queue(queue)
            return removed
        return None

    def pop_queue_front(self) -> Optional[Dict[str, Any]]:
        """Pop first item from queue."""
        queue = self.get_queue()
        if queue:
            item = queue.pop(0)
            self.set_queue(queue)
            return item
        return None

    def clear_queue(self) -> None:
        """Clear entire queue."""
        self.set_queue([])

    def get_queue_length(self) -> int:
        return len(self.get_queue())

    # ── Queue Lock ──────────────────────────────────────────

    def is_queue_locked(self) -> bool:
        return self.redis.get('radio:queue_locked') == '1'

    def set_queue_locked(self, locked: bool) -> None:
        if locked:
            self.redis.set('radio:queue_locked', '1')
        else:
            self.redis.delete('radio:queue_locked')

    # ── History (Redis cache for fast access) ───────────────

    def get_recent_history(self, limit: int = 50) -> List[str]:
        """Get recently played video IDs."""
        data = self.redis.lrange('radio:history', 0, limit - 1)
        return data

    def add_to_history(self, video_id: str) -> None:
        """Add to recent history."""
        self.redis.lpush('radio:history', video_id)
        # Keep last 100 only
        self.redis.ltrim('radio:history', 0, 99)

    # ── Admin Override ──────────────────────────────────────

    def get_admin_force_play(self) -> Optional[str]:
        """Get admin-forced video ID."""
        data = self.redis.get('radio:admin_force')
        return data

    def set_admin_force_play(self, video_id: str) -> None:
        self.redis.set('radio:admin_force', video_id)

    def clear_admin_force_play(self) -> None:
        self.redis.delete('radio:admin_force')

    def get_admin_skip(self) -> bool:
        """Check and consume admin skip flag."""
        val = self.redis.get('radio:admin_skip')
        if val == '1':
            self.redis.delete('radio:admin_skip')
            return True
        return False

    def set_admin_skip(self) -> None:
        self.redis.set('radio:admin_skip', '1')

    # ── User Queue Rate Limit ───────────────────────────────

    def check_user_queue_limit(self, user_id: str) -> bool:
        """Returns True if user can still add to queue (1 song per 3 minutes)."""
        key = f'radio:user_queue:{user_id}'
        count = self.redis.get(key)
        if count is None:
            return True
        return int(count) < 1  # Max 1 song

    def increment_user_queue_count(self, user_id: str) -> None:
        """Increment user's queue count. 3-minute cooldown."""
        key = f'radio:user_queue:{user_id}'
        self.redis.incr(key)
        self.redis.expire(key, 180)  # 3 minute cooldown

    # ── Vote Skip ────────────────────────────────────────────

    def add_skip_vote(self, client_id: str) -> int:
        """Add a skip vote. Returns total vote count. Uses Redis SET for dedup."""
        self.redis.sadd('radio:skip_votes', client_id)
        return self.redis.scard('radio:skip_votes')

    def get_skip_votes(self) -> int:
        return self.redis.scard('radio:skip_votes')

    def clear_skip_votes(self) -> None:
        self.redis.delete('radio:skip_votes')

    # ── Engine Start Time ────────────────────────────────────

    def set_engine_start_time(self):
        self.redis.set('radio:engine_start', str(time.time()))

    def get_engine_start_time(self) -> Optional[float]:
        val = self.redis.get('radio:engine_start')
        return float(val) if val else None
