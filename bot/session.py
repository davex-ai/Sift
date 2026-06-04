"""
Session — per-user state for the Telegram bot.

Tracks:
  - Last search results (for pagination)
  - Last query (for re-runs and alerts)
  - Page cursor (Next 5 / Prev 5)
  - Rate limit counter
  - Active alert setup flow state
"""

import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict, deque

import config

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    user_id: int
    username: str = ""

    # Last search
    last_query: str = ""
    last_groups: list = field(default_factory=list)     # ProductGroup list
    last_intent = None                                   # SearchIntent

    # Pagination
    page: int = 0                                        # current page (0-indexed)

    # Alert flow
    alert_pending_product_id: Optional[str] = None      # product user selected
    alert_pending_title: str = ""
    awaiting_threshold: bool = False

    # Rate limiting
    request_times: deque = field(
        default_factory=lambda: deque(maxlen=config.BOT_RATE_LIMIT_PER_USER * 2)
    )

    def current_page_groups(self):
        start = self.page * config.BOT_PAGE_SIZE
        end = start + config.BOT_PAGE_SIZE
        return self.last_groups[start:end]

    def has_next_page(self) -> bool:
        return (self.page + 1) * config.BOT_PAGE_SIZE < len(self.last_groups)

    def has_prev_page(self) -> bool:
        return self.page > 0

    def total_pages(self) -> int:
        import math
        return max(1, math.ceil(len(self.last_groups) / config.BOT_PAGE_SIZE))

    def is_rate_limited(self) -> bool:
        now = time.monotonic()
        # Purge timestamps older than 60s
        while self.request_times and self.request_times[0] < now - 60:
            self.request_times.popleft()
        return len(self.request_times) >= config.BOT_RATE_LIMIT_PER_USER

    def record_request(self) -> None:
        self.request_times.append(time.monotonic())

    def time_until_allowed(self) -> float:
        """Seconds until next request is allowed."""
        now = time.monotonic()
        if len(self.request_times) < config.BOT_RATE_LIMIT_PER_USER:
            return 0.0
        oldest = self.request_times[0]
        return max(0.0, 60.0 - (now - oldest))

    def reset_pagination(self) -> None:
        self.page = 0

    def reset_alert_flow(self) -> None:
        self.alert_pending_product_id = None
        self.alert_pending_title = ""
        self.awaiting_threshold = False


class SessionManager:
    """Thread-safe store of UserSession objects."""

    def __init__(self):
        self._sessions: dict[int, UserSession] = {}
        self._lock = threading.Lock()

    def get(self, user_id: int, username: str = "") -> UserSession:
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = UserSession(
                    user_id=user_id,
                    username=username,
                )
                logger.debug(f"[Session] New session: {user_id}")
            return self._sessions[user_id]

    def clear(self, user_id: int) -> None:
        with self._lock:
            self._sessions.pop(user_id, None)

    def all_user_ids(self) -> list[int]:
        with self._lock:
            return list(self._sessions.keys())


# Singleton
_manager = SessionManager()

def get_session(user_id: int, username: str = "") -> UserSession:
    return _manager.get(user_id, username)

def clear_session(user_id: int) -> None:
    _manager.clear(user_id)

def all_user_ids() -> list[int]:
    return _manager.all_user_ids()
