"""
Intent Parser — understands what the user is searching for.

Extracts:
  - Cleaned product query (no budget/modifier fluff)
  - Budget ceiling (NGN)
  - Category hint (phone, laptop, blender, etc.)
  - Comparison mode ('cheapest', 'best value', 'highest rated')
  - Store filters (if user specified a store)
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from utils.currency import extract_budget

logger = logging.getLogger(__name__)


# ── Category keywords → category label ───────────────────────
_CATEGORIES = {
    "phone": ["phone", "iphone", "samsung", "tecno", "infinix", "smartphone", "android"],
    "laptop": ["laptop", "notebook", "macbook", "chromebook", "pc", "computer"],
    "tablet": ["tablet", "ipad", "tab"],
    "tv": ["tv", "television", "smart tv", "led tv", "oled"],
    "blender": ["blender", "mixer", "food processor", "juicer"],
    "fridge": ["fridge", "refrigerator", "freezer", "cooler"],
    "ac": ["air conditioner", "air con", "ac unit", "split unit"],
    "generator": ["generator", "gen set", "inverter"],
    "headphone": ["headphone", "earphone", "earbud", "airpod", "headset", "tws"],
    "speaker": ["speaker", "soundbar", "bluetooth speaker"],
    "camera": ["camera", "dslr", "mirrorless", "webcam"],
    "watch": ["smartwatch", "watch", "wristwatch"],
    "shoe": ["shoe", "sneaker", "sandal", "boot"],
    "clothing": ["shirt", "dress", "trouser", "skirt", "jean", "cloth"],
    "perfume": ["perfume", "cologne", "fragrance"],
    "baby": ["baby", "diaper", "nappy", "pram", "stroller", "formula"],
    "kitchen": ["pot", "pan", "cookware", "kettle", "rice cooker", "microwave"],
    "power_bank": ["power bank", "powerbank", "portable charger"],
    "charger": ["charger", "adapter", "cable", "usb"],
    "gaming": ["console", "ps5", "playstation", "xbox", "nintendo", "gaming"],
}

# ── Comparison intent words ───────────────────────────────────
_COMPARISON_MODES = {
    "cheapest": ["cheapest", "lowest price", "most affordable", "budget"],
    "best_value": ["best value", "best deal", "worth", "value for money"],
    "highest_rated": ["best rated", "highest rated", "top rated", "best quality"],
    "newest": ["latest", "newest", "most recent", "new release"],
}

# ── Budget trigger words (not part of the search query) ───────
_BUDGET_PREFIXES = [
    r"\bunder\b", r"\bbelow\b", r"\bless than\b",
    r"\bmax\b", r"\bbefore\b",
    r"\baround\b", r"\babout\b",
    r"\bbudget\b",
]


@dataclass
class SearchIntent:
    raw_query: str
    clean_query: str                    # query with budget/modifiers stripped
    budget_ngn: Optional[float] = None
    category: Optional[str] = None
    comparison_mode: str = "best_value"
    store_filter: list[str] = field(default_factory=list)

    def __str__(self):
        parts = [f"query='{self.clean_query}'"]
        if self.budget_ngn:
            parts.append(f"budget=₦{self.budget_ngn:,.0f}")
        if self.category:
            parts.append(f"category={self.category}")
        if self.store_filter:
            parts.append(f"stores={self.store_filter}")
        parts.append(f"mode={self.comparison_mode}")
        return f"Intent({', '.join(parts)})"


class IntentParser:

    def parse(self, user_text: str) -> SearchIntent:
        raw = user_text.strip()
        q = raw.lower()

        budget = extract_budget(q)
        category = self._detect_category(q)
        mode = self._detect_mode(q)
        store_filter = self._detect_store_filter(q)
        clean = self._clean_query(raw, budget)

        intent = SearchIntent(
            raw_query=raw,
            clean_query=clean,
            budget_ngn=budget,
            category=category,
            comparison_mode=mode,
            store_filter=store_filter,
        )

        logger.info(f"[Intent] {intent}")
        return intent

    def _detect_category(self, q: str) -> Optional[str]:
        for category, keywords in _CATEGORIES.items():
            for kw in keywords:
                if kw in q:
                    return category
        return None

    def _detect_mode(self, q: str) -> str:
        for mode, keywords in _COMPARISON_MODES.items():
            for kw in keywords:
                if kw in q:
                    return mode
        return "best_value"

    def _detect_store_filter(self, q: str) -> list[str]:
        stores = []
        store_names = {
            "jumia": ["jumia"],
            "konga": ["konga"],
            "slot": ["slot"],
            "jiji": ["jiji"],
            "temu": ["temu"],
        }
        for store, aliases in store_names.items():
            if any(alias in q for alias in aliases):
                stores.append(store)
        return stores

    def _clean_query(self, raw: str, budget: Optional[float]) -> str:
        """Remove budget clause and modifier words from query."""
        q = raw

        # Remove budget expression
        budget_patterns = [
            r"\b(?:under|below|less\s+than|max|not\s+more\s+than)\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"\bbudget\s*(?:of|:)?\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"\baround\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"[₦#]\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?",
        ]
        for pattern in budget_patterns:
            q = re.sub(pattern, "", q, flags=re.IGNORECASE)

        # Remove comparison mode modifiers
        mode_words = [
            r"\bbest\s+value\b", r"\bbest\s+deal\b", r"\bcheapest\b",
            r"\bmost\s+affordable\b", r"\bbest\s+rated\b", r"\btop\s+rated\b",
            r"\blatest\b", r"\bnewest\b",
        ]
        for pattern in mode_words:
            q = re.sub(pattern, "", q, flags=re.IGNORECASE)

        # Remove store names if mentioned
        store_words = r"\b(?:on|from|at)\s+(?:jumia|konga|slot|jiji|temu)\b"
        q = re.sub(store_words, "", q, flags=re.IGNORECASE)

        # Normalize whitespace
        q = re.sub(r"\s+", " ", q).strip(" -,.")

        return q or raw  # fallback to original if we stripped everything
