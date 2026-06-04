"""
QueryCache — in-memory LRU cache with TTL.

Caches (clean_query → ProductGroups) for config.CACHE_TTL_SECONDS.
Returns stale result immediately while triggering a background refresh.
This keeps the bot feeling fast.

Upgrade path: swap for Redis with the same interface.
"""

import time
import logging
import threading
from collections import OrderedDict
from typing import Optional

import config

logger = logging.getLogger(__name__)


class CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value, ttl: int):
        self.value = value
        self.expires_at = time.monotonic() + ttl


class QueryCache:
    """
    Thread-safe in-memory cache.
    Evicts LRU entries when max_entries is reached.
    """

    def __init__(
        self,
        ttl: int = config.CACHE_TTL_SECONDS,
        max_entries: int = config.CACHE_MAX_ENTRIES,
    ):
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[list]:
        with self._lock:
            entry = self._store.get(self._normalize_key(key))
            if entry is None:
                return None

            if time.monotonic() > entry.expires_at:
                # Expired — but still return stale value (caller gets fast response)
                logger.debug(f"[Cache] Stale hit: '{key}'")
                return entry.value  # background refresh is caller's responsibility

            # Move to end (LRU touch)
            self._store.move_to_end(self._normalize_key(key))
            logger.debug(f"[Cache] Hit: '{key}'")
            return entry.value

    def set(self, key: str, value) -> None:
        k = self._normalize_key(key)
        with self._lock:
            # Evict LRU if full
            while len(self._store) >= self.max_entries:
                evicted_key, _ = self._store.popitem(last=False)
                logger.debug(f"[Cache] Evicted: '{evicted_key}'")

            self._store[k] = CacheEntry(value, self.ttl)
            self._store.move_to_end(k)
            logger.debug(f"[Cache] Set: '{key}' (TTL={self.ttl}s)")

    def invalidate(self, key: str) -> None:
        k = self._normalize_key(key)
        with self._lock:
            self._store.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
        logger.info("[Cache] Cleared")

    def stats(self) -> dict:
        with self._lock:
            total = len(self._store)
            now = time.monotonic()
            fresh = sum(1 for e in self._store.values() if e.expires_at > now)
            return {"total": total, "fresh": fresh, "stale": total - fresh}

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Lowercase + strip for consistent hits."""
        return key.lower().strip()
