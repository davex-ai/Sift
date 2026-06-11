"""
db/cache.py

QueryCache — in-memory LRU cache with TTL.

Changes from previous version:
  - get/set now accept the string key produced by intent.cache_key()
    (an MD5 hash of query + budget + brand + stores)
  - This means "laptop under 300k" and "laptop under 150k" are
    separate cache entries and never cross-contaminate
  - No other changes — interface is backward compatible
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
        self.value     = value
        self.expires_at = time.monotonic() + ttl


class QueryCache:
    """
    Thread-safe LRU cache keyed on intent.cache_key().

    The key is an MD5 hash of:
      query + budget_max + budget_min + brand + stores + mode

    So two structurally different intents with the same query string
    get separate entries and never see each other's results.
    """

    def __init__(
        self,
        ttl: int = config.CACHE_TTL_SECONDS,
        max_entries: int = config.CACHE_MAX_ENTRIES,
    ):
        self.ttl         = ttl
        self.max_entries = max_entries
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock  = threading.Lock()

    def get(self, key: str) -> Optional[list]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                logger.debug(f"[Cache] Stale hit: {key[:8]}…")
                return entry.value   # return stale; pipeline will refresh async
            self._store.move_to_end(key)
            logger.debug(f"[Cache] Hit: {key[:8]}…")
            return entry.value

    def set(self, key: str, value) -> None:
        with self._lock:
            while len(self._store) >= self.max_entries:
                evicted, _ = self._store.popitem(last=False)
                logger.debug(f"[Cache] Evicted: {evicted[:8]}…")
            self._store[key] = CacheEntry(value, self.ttl)
            self._store.move_to_end(key)
            logger.debug(f"[Cache] Set: {key[:8]}… (TTL={self.ttl}s)")

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
        logger.info("[Cache] Cleared")

    def stats(self) -> dict:
        with self._lock:
            total = len(self._store)
            now   = time.monotonic()
            fresh = sum(1 for e in self._store.values() if e.expires_at > now)
            return {"total": total, "fresh": fresh, "stale": total - fresh}