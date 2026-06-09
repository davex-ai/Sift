"""
bot/session.py

Per-user state for the Telegram bot.

Tracks:
  - Last search results (pagination)
  - Rate limiting
  - Alert flow
  - Multi-turn conversation session for clarification
  - Search analytics counters
"""

import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional
from collections import deque
from datetime import datetime

import config

logger = logging.getLogger(__name__)


# ── Conversation clarification session ───────────────────────

@dataclass
class ConversationSession:
    """
    Tracks partial intent across multiple turns.

    Flow:
      User: "I need a laptop"
      → category=laptop, bot asks budget
      User: "300k"
      → budget=300000, bot asks brand
      User: "Dell"
      → brand=Dell, now search

    awaiting_field: which field we're waiting for next
    Possible values: "budget" | "brand" | "ram" | "storage" | None
    """
    category: Optional[str] = None
    budget_max: Optional[float] = None
    budget_min: Optional[float] = None
    brand: Optional[str] = None
    storage: Optional[str] = None
    ram: Optional[str] = None

    # What we asked the user for last
    awaiting_field: Optional[str] = None
    # The partial query the user started with
    base_query: str = ""
    # How many clarification turns we've done (cap at 2 to avoid annoying loops)
    turns: int = 0

    updated_at: float = field(default_factory=time.monotonic)

    def is_active(self) -> bool:
        """Session expires after 3 minutes of inactivity."""
        return (
            self.awaiting_field is not None
            and (time.monotonic() - self.updated_at) < 180
        )

    def touch(self):
        self.updated_at = time.monotonic()

    def build_query(self) -> str:
        """Assemble final search query from collected fields."""
        parts = []
        if self.brand:
            parts.append(self.brand)
        if self.category:
            parts.append(self.category)
        elif self.base_query:
            parts.append(self.base_query)
        if self.storage:
            parts.append(self.storage)
        if self.ram:
            parts.append(self.ram + " RAM")
        return " ".join(parts) or self.base_query

    def reset(self):
        self.category = None
        self.budget_max = None
        self.budget_min = None
        self.brand = None
        self.storage = None
        self.ram = None
        self.awaiting_field = None
        self.base_query = ""
        self.turns = 0
        self.updated_at = time.monotonic()


# ── Which categories warrant clarification questions ─────────
# Maps category → ordered list of fields to ask about
# Stop asking after 2 questions or when we have enough to search

CLARIFICATION_FLOW = {
    "laptop":   ["budget", "brand"],
    "phone":    ["budget", "brand"],
    "tablet":   ["budget", "brand"],
    "tv":       ["budget"],
    "fridge":   ["budget"],
    "ac":       ["budget"],
    "camera":   ["budget"],
    "headphone":["budget"],
    "speaker":  ["budget"],
    "gaming":   ["budget"],
    "blender":  ["budget"],
    "furniture":["budget"],
}

FIELD_QUESTIONS = {
    "budget":  "What's your budget? (e.g. 150k, ₦200,000)",
    "brand":   "Any preferred brand? (e.g. Samsung, Dell, HP — or type *any* to skip)",
    "storage": "Storage size? (e.g. 256GB, 512GB — or *any* to skip)",
    "ram":     "How much RAM? (e.g. 8GB, 16GB — or *any* to skip)",
}


def get_next_clarification_field(conv: ConversationSession) -> Optional[str]:
    """
    Returns the next field to ask about, or None if we have enough to search.
    Skips fields already filled.
    """
    if not conv.category:
        return None
    if conv.turns >= 2:  # cap — don't over-interrogate
        return None

    flow = CLARIFICATION_FLOW.get(conv.category, [])
    for field_name in flow:
        already_filled = getattr(conv, field_name.replace("budget", "budget_max"), None)
        if field_name == "budget" and conv.budget_max:
            continue
        if field_name == "brand" and conv.brand:
            continue
        if field_name == "storage" and conv.storage:
            continue
        if field_name == "ram" and conv.ram:
            continue
        return field_name  # first unfilled field

    return None  # all filled — ready to search


# ══════════════════════════════════════════════════════════════
# Main user session
# ══════════════════════════════════════════════════════════════

@dataclass
class UserSession:
    user_id: int
    username: str = ""

    # Last search
    last_query: str = ""
    last_groups: list = field(default_factory=list)
    last_intent = None

    # Pagination
    page: int = 0

    # Alert flow
    alert_pending_product_id: Optional[str] = None
    alert_pending_title: str = ""
    awaiting_threshold: bool = False

    # Conversation / clarification session
    conversation: ConversationSession = field(default_factory=ConversationSession)

    # Rate limiting
    request_times: deque = field(
        default_factory=lambda: deque(maxlen=config.BOT_RATE_LIMIT_PER_USER * 2)
    )

    # Analytics
    total_searches: int = 0
    first_seen: float = field(default_factory=time.monotonic)

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
        while self.request_times and self.request_times[0] < now - 60:
            self.request_times.popleft()
        return len(self.request_times) >= config.BOT_RATE_LIMIT_PER_USER

    def record_request(self) -> None:
        self.request_times.append(time.monotonic())
        self.total_searches += 1

    def time_until_allowed(self) -> float:
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

    def start_conversation(self, category: str, base_query: str) -> None:
        """Begin a clarification session."""
        self.conversation.reset()
        self.conversation.category = category
        self.conversation.base_query = base_query
        self.conversation.touch()

    def end_conversation(self) -> None:
        """Clear the clarification session."""
        self.conversation.reset()


class SessionManager:
    def __init__(self):
        self._sessions: dict[int, UserSession] = {}
        self._lock = threading.Lock()

    def get(self, user_id: int, username: str = "") -> UserSession:
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = UserSession(user_id=user_id, username=username)
                logger.debug(f"[Session] New session: {user_id}")
            return self._sessions[user_id]

    def clear(self, user_id: int) -> None:
        with self._lock:
            self._sessions.pop(user_id, None)

    def all_user_ids(self) -> list[int]:
        with self._lock:
            return list(self._sessions.keys())

    def all_sessions(self) -> list[UserSession]:
        with self._lock:
            return list(self._sessions.values())


_manager = SessionManager()


def get_session(user_id: int, username: str = "") -> UserSession:
    return _manager.get(user_id, username)

def clear_session(user_id: int) -> None:
    _manager.clear(user_id)

def all_user_ids() -> list[int]:
    return _manager.all_user_ids()

def all_sessions() -> list[UserSession]:
    return _manager.all_sessions()