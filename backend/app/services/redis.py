import redis
import json
from typing import Optional, Any
import os


class RedisService:
    """Redis service for state management"""
    
    def __init__(self):
        self.client = redis.Redis(
            host=os.getenv('REDIS_HOST', 'localhost'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            db=int(os.getenv('REDIS_DB', 0)),
            decode_responses=True
        )
    
    def get_radio_state(self) -> Optional[dict]:
        """Get current radio state"""
        data = self.client.get('radio:state')
        return json.loads(data) if data else None
    
    def set_radio_state(self, state: dict) -> None:
        """Set radio state"""
        self.client.set('radio:state', json.dumps(state))
    
    def get_queue(self) -> list:
        """Get queue items"""
        items = self.client.lrange('radio:queue', 0, -1)
        return [json.loads(item) for item in items]
    
    def add_to_queue(self, item: dict) -> None:
        """Add item to queue"""
        self.client.rpush('radio:queue', json.dumps(item))
    
    def pop_queue(self) -> Optional[dict]:
        """Pop first item from queue"""
        item = self.client.lpop('radio:queue')
        return json.loads(item) if item else None
    
    def clear_queue(self) -> None:
        """Clear entire queue"""
        self.client.delete('radio:queue')
    
    def add_to_history(self, track_id: str) -> None:
        """Add track to play history (keep last 50)"""
        self.client.lpush('radio:history', track_id)
        self.client.ltrim('radio:history', 0, 49)
    
    def get_history(self, limit: int = 10) -> list:
        """Get play history"""
        return self.client.lrange('radio:history', 0, limit - 1)
    
    def is_in_recent_history(self, track_id: str, check_last: int = 20) -> bool:
        """Check if track was played recently"""
        history = self.client.lrange('radio:history', 0, check_last - 1)
        return track_id in history
    
    def ping(self) -> bool:
        """Check Redis connection"""
        try:
            return self.client.ping()
        except:
            return False


# Singleton instance
redis_service = RedisService()
