import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))


class Config:
    BASE_DIR: str = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    ADMIN_PASSWORD: str = os.getenv('ADMIN_PASSWORD', 'admin123')
    JWT_SECRET: str = os.getenv('JWT_SECRET', 'dev-secret')
    REDIS_URL: str = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    CROSSFADE_MS: int = int(os.getenv('CROSSFADE_MS', '3000'))
    DRIFT_CHECK_INTERVAL_S: int = int(os.getenv('DRIFT_CHECK_INTERVAL_S', '3'))
    DRIFT_THRESHOLD_MS: int = int(os.getenv('DRIFT_THRESHOLD_MS', '500'))
    NEXT_TRACK_NOTIFY_S: int = int(os.getenv('NEXT_TRACK_NOTIFY_S', '20'))
    QUEUE_USER_LIMIT: int = int(os.getenv('QUEUE_USER_LIMIT', '5'))
    DB_PATH: str = os.getenv(
        'DB_PATH',
        os.path.join(os.path.dirname(__file__), '..', 'radiotify.db'),
    )
    AUDIO_CACHE_DIR: str = os.getenv(
        'AUDIO_CACHE_DIR',
        os.path.join(os.path.dirname(__file__), '..', 'audio_cache'),
    )


config = Config()
