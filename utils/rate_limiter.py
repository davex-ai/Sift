"""
Rate Limiter — Token Bucket + Semaphore.

Each scraper gets its own RateLimiter instance.
Controls:
  - Max N requests per time window (token bucket)
  - Max concurrent connections (semaphore)
  - Randomized delay between requests
"""

import time
import random
import threading
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Thread-safe token bucket rate limiter + concurrency limiter.

    Usage:
        limiter = RateLimiter(max_rpm=5, max_concurrent=3)

        with limiter.throttle():
            response = requests.get(url)
    """

    def __init__(
        self,
        max_rpm: int = 5,               # max requests per minute
        max_concurrent: int = 3,        # max parallel connections
        min_delay: float = 1.2,         # minimum seconds between requests
        max_delay: float = 3.5,         # maximum seconds between requests
        jitter: float = 0.8,            # extra random seconds
    ):
        self.max_rpm = max_rpm
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.jitter = jitter

        # Token bucket state
        self._lock = threading.Lock()
        self._tokens = float(max_rpm)
        self._max_tokens = float(max_rpm)
        self._refill_rate = max_rpm / 60.0      # tokens per second
        self._last_refill = time.monotonic()
        self._last_request_time: float = 0.0

        # Concurrency limit
        self._semaphore = threading.Semaphore(max_concurrent)

    def _refill(self) -> None:
        """Add tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self._refill_rate
        self._tokens = min(self._max_tokens, self._tokens + new_tokens)
        self._last_refill = now

    def _wait_for_token(self) -> None:
        """Block until a token is available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            # Wait a fraction of a second before retrying
            time.sleep(0.2)

    def _apply_delay(self) -> None:
        """
        Enforce minimum spacing between requests with randomization.
        This is separate from the token bucket — it's about looking human.
        """
        with self._lock:
            now = time.monotonic()
            since_last = now - self._last_request_time

            # Base delay (randomized)
            base = random.uniform(self.min_delay, self.max_delay)
            jitter_extra = random.uniform(0, self.jitter)
            desired_delay = base + jitter_extra

            remaining = desired_delay - since_last
            if remaining > 0:
                self._last_request_time = now + remaining
            else:
                self._last_request_time = now

        if remaining > 0:
            logger.debug(f"[RateLimiter] Sleeping {remaining:.2f}s")
            time.sleep(remaining)

    @contextmanager
    def throttle(self):
        """
        Context manager: acquire rate limit + concurrency slot, then release.

        Usage:
            with limiter.throttle():
                do_request()
        """
        # 1. Wait for concurrency slot
        self._semaphore.acquire()
        try:
            # 2. Wait for token (rate limit)
            self._wait_for_token()
            # 3. Human-like delay
            self._apply_delay()
            yield
        finally:
            self._semaphore.release()


class GlobalConcurrencyLimiter:
    """
    Shared semaphore across all scrapers.
    Prevents total concurrent requests from exceeding max_total.
    """

    def __init__(self, max_total: int = 5):
        self._semaphore = threading.Semaphore(max_total)

    @contextmanager
    def limit(self):
        self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()


# Singleton — all scrapers share this
global_limiter = GlobalConcurrencyLimiter(max_total=5)
