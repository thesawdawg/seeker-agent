"""
Session storage — shared across all auth backends.

Sessions live in Redis when REDIS_URL is set, and in process memory
otherwise, so a single-process deployment needs no Redis.
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = int(os.environ.get("SEEKER_SESSION_TTL", "86400"))
SESSION_HEADER = "x-seeker-session"


class MemorySessions:
    """Fallback store. Fine for one process; Redis is required beyond that."""

    def __init__(self):
        self._data: dict[str, tuple[float, dict]] = {}

    def set(self, token: str, payload: dict, ttl: int) -> None:
        self._data[token] = (time.time() + ttl, payload)

    def get(self, token: str) -> Optional[dict]:
        entry = self._data.get(token)
        if not entry:
            return None
        expires, payload = entry
        if time.time() > expires:
            self._data.pop(token, None)
            return None
        return payload

    def delete(self, token: str) -> None:
        self._data.pop(token, None)


class RedisSessions:
    def __init__(self, url: str):
        import redis
        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def set(self, token: str, payload: dict, ttl: int) -> None:
        self._redis.setex(f"seeker:session:{token}", ttl, json.dumps(payload))

    def get(self, token: str) -> Optional[dict]:
        raw = self._redis.get(f"seeker:session:{token}")
        return json.loads(raw) if raw else None

    def delete(self, token: str) -> None:
        self._redis.delete(f"seeker:session:{token}")


_sessions = None


def sessions():
    global _sessions
    if _sessions is None:
        url = os.environ.get("REDIS_URL", "").strip()
        if url:
            try:
                _sessions = RedisSessions(url)
                logger.info(f"Sessions in Redis: {url}")
            except Exception as e:
                logger.error(f"Redis unavailable ({e}) — using in-memory sessions")
                _sessions = MemorySessions()
        else:
            logger.info("REDIS_URL unset — using in-memory sessions")
            _sessions = MemorySessions()
    return _sessions


def reset_sessions():
    """Test hook."""
    global _sessions
    _sessions = None
