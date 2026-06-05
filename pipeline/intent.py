"""
pipeline/intent.py

Two-stage intent parser:
  Stage 1 — rule-based (always runs, zero cost)
  Stage 2 — LLM fallback (only when confidence < 0.45)

SearchIntent now carries both budget_min and budget_max
to support range queries like "between ₦900k and ₦2m".
"""

import re
import json
import logging
import requests
from dataclasses import dataclass, field
from typing import Optional

from utils.currency import extract_budget
import config

logger = logging.getLogger(__name__)


# ── Category keyword table ────────────────────────────────────
_CATEGORIES = {
    "phone":       ["phone", "iphone", "samsung", "tecno", "infinix", "smartphone", "android"],
    "laptop":      ["laptop", "notebook", "macbook", "chromebook", "pc", "computer"],
    "tablet":      ["tablet", "ipad", "tab"],
    "tv":          ["tv", "television", "smart tv", "led tv", "oled"],
    "blender":     ["blender", "mixer", "food processor", "juicer"],
    "fridge":      ["fridge", "refrigerator", "freezer", "cooler"],
    "ac":          ["air conditioner", "air con", "ac unit", "split unit"],
    "generator":   ["generator", "gen set", "inverter"],
    "headphone":   ["headphone", "earphone", "earbud", "airpod", "headset", "tws"],
    "speaker":     ["speaker", "soundbar", "bluetooth speaker"],
    "camera":      ["camera", "dslr", "mirrorless", "webcam", "cctv", "security camera", "ip camera"],
    "watch":       ["smartwatch", "watch", "wristwatch"],
    "shoe":        ["shoe", "sneaker", "sandal", "boot"],
    "clothing":    ["shirt", "dress", "trouser", "skirt", "jean", "cloth"],
    "perfume":     ["perfume", "cologne", "fragrance"],
    "baby":        ["baby", "diaper", "nappy", "pram", "stroller", "formula"],
    "kitchen":     ["pot", "pan", "cookware", "kettle", "rice cooker", "microwave", "toaster"],
    "power_bank":  ["power bank", "powerbank", "portable charger"],
    "charger":     ["charger", "adapter", "cable", "usb hub"],
    "gaming":      ["console", "ps5", "playstation", "xbox", "nintendo", "gaming"],
    "toilet":      ["toilet", "wc", "bathroom set", "toilet set", "water closet"],
    "smart_home":  ["smart home", "home automation", "smart plug", "smart bulb",
                    "home monitor", "siri", "alexa", "google home", "homekit"],
    "furniture":   ["sofa", "chair", "table", "bed", "wardrobe", "shelf", "furniture"],
    "mattress":    ["mattress", "foam", "spring bed"],
}

_COMPARISON_MODES = {
    "cheapest":      ["cheapest", "lowest price", "most affordable"],
    "best_value":    ["best value", "best deal", "worth", "value for money"],
    "highest_rated": ["best rated", "highest rated", "top rated", "best quality"],
    "newest":        ["latest", "newest", "most recent", "new release"],
}


# ══════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════

@dataclass
class SearchIntent:
    raw_query: str
    clean_query: str

    budget_max_ngn: Optional[float] = None   # "under ₦50k"
    budget_min_ngn: Optional[float] = None   # "more than ₦900k"

    # Convenience alias
    @property
    def budget_ngn(self) -> Optional[float]:
        return self.budget_max_ngn

    category: Optional[str] = None
    comparison_mode: str = "best_value"
    store_filter: list = field(default_factory=list)

    # Parser metadata
    parsed_by: str = "rules"             # "rules" | "llm"
    confidence: float = 1.0

    def __str__(self):
        parts = [f"query='{self.clean_query}'"]
        if self.budget_max_ngn:
            parts.append(f"max=₦{self.budget_max_ngn:,.0f}")
        if self.budget_min_ngn:
            parts.append(f"min=₦{self.budget_min_ngn:,.0f}")
        if self.category:
            parts.append(f"category={self.category}")
        parts.append(f"mode={self.comparison_mode}")
        parts.append(f"by={self.parsed_by}({self.confidence:.2f})")
        return f"Intent({', '.join(parts)})"


# ══════════════════════════════════════════════════════════════
# Confidence scoring
# ══════════════════════════════════════════════════════════════

def _confidence(raw: str, clean: str, category: Optional[str], budget_max: Optional[float]) -> float:
    """
    Score how confident the rule parser is about this query (0.0 → 1.0).

    Signals that lower confidence:
      - No category matched
      - Very short clean query after stripping (leftover noise)
      - Indirect phrasing: "something that", "works with", "compatible with"
      - No budget and no clear product noun
    """
    score = 0.0

    # Category match is the strongest signal
    if category:
        score += 0.40

    # Budget found → query is structured
    if budget_max:
        score += 0.20

    # Clean query is meaningful (not just a qualifier remnant)
    words = [w for w in clean.lower().split() if len(w) >= 3]
    if len(words) >= 2:
        score += 0.20
    elif len(words) == 1:
        score += 0.10

    # Indirect phrasing signals LLM is needed
    indirect_patterns = [
        r"\bsomething\s+(that|to|which|for)\b",
        r"\bworks\s+with\b",
        r"\bcompatible\s+with\b",
        r"\bworks\s+on\b",
        r"\bi\s+(want|need|am looking for|am searching for)\b",
        r"\blooking\s+for\b",
        r"\bhelp\s+me\s+find\b",
        r"\bwhat.*good.*for\b",
    ]
    q = raw.lower()
    if any(re.search(p, q) for p in indirect_patterns):
        score -= 0.25

    # Clean query is very short after stripping (suggests over-stripping)
    if len(clean.strip()) < 4:
        score -= 0.30

    return round(max(0.0, min(1.0, score)), 2)


# ══════════════════════════════════════════════════════════════
# Min-budget extractor
# ══════════════════════════════════════════════════════════════

def _extract_min_budget(q: str) -> Optional[float]:
    """
    Extract lower price bound from phrases like:
      "more than ₦900k", "above 1 million", "at least ₦500,000"
    """
    patterns = [
        r"(?:more\s+than|above|over|at\s+least|minimum|min|greater\s+than)\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
        r"(?:from|starting\s+(?:from|at))\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
    ]
    for pattern in patterns:
        m = re.search(pattern, q, re.IGNORECASE)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
                mult = (m.group(2) or "").lower()
                if mult in ("k", "thousand"):
                    amount *= 1_000
                elif mult in ("m", "million"):
                    amount *= 1_000_000
                if amount > 0:
                    return amount
            except ValueError:
                continue
    return None


# ══════════════════════════════════════════════════════════════
# Rule-based parser
# ══════════════════════════════════════════════════════════════

class _RuleParser:

    def parse(self, user_text: str) -> SearchIntent:
        raw = user_text.strip()
        q = raw.lower()

        budget_max = extract_budget(q)
        budget_min = _extract_min_budget(q)
        category   = self._detect_category(q)
        mode       = self._detect_mode(q)
        stores     = self._detect_stores(q)
        clean      = self._clean_query(raw, budget_max, budget_min)
        conf       = _confidence(raw, clean, category, budget_max)

        return SearchIntent(
            raw_query=raw,
            clean_query=clean,
            budget_max_ngn=budget_max,
            budget_min_ngn=budget_min,
            category=category,
            comparison_mode=mode,
            store_filter=stores,
            parsed_by="rules",
            confidence=conf,
        )

    def _detect_category(self, q: str) -> Optional[str]:
        for cat, keywords in _CATEGORIES.items():
            for kw in keywords:
                if kw in q:
                    return cat
        return None

    def _detect_mode(self, q: str) -> str:
        for mode, keywords in _COMPARISON_MODES.items():
            for kw in keywords:
                if kw in q:
                    return mode
        return "best_value"

    def _detect_stores(self, q: str) -> list:
        stores = []
        for store in ["jumia", "konga", "slot", "jiji", "temu"]:
            if store in q:
                stores.append(store)
        return stores

    def _clean_query(
        self,
        raw: str,
        budget_max: Optional[float],
        budget_min: Optional[float],
    ) -> str:
        q = raw

        # Strip max-budget phrases
        for pattern in [
            r"\b(?:under|below|less\s+than|max|not\s+more\s+than)\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"\bbudget\s*(?:of|:)?\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"\baround\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"[₦#]\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?",
        ]:
            q = re.sub(pattern, "", q, flags=re.IGNORECASE)

        # Strip min-budget phrases
        for pattern in [
            r"\b(?:more\s+than|above|over|at\s+least|minimum|min|greater\s+than)\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
            r"\b(?:from|starting\s+(?:from|at))\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
        ]:
            q = re.sub(pattern, "", q, flags=re.IGNORECASE)

        # Strip "but more than X" connector
        q = re.sub(r"\bbut\s+(?:more|greater|above|over)\b", "", q, flags=re.IGNORECASE)

        # Strip mode qualifiers
        for pattern in [
            r"\bbest\s+value\b", r"\bbest\s+deal\b", r"\bcheapest\b",
            r"\bmost\s+affordable\b", r"\bbest\s+rated\b", r"\btop\s+rated\b",
            r"\blatest\b", r"\bnewest\b",
        ]:
            q = re.sub(pattern, "", q, flags=re.IGNORECASE)

        # Strip store names
        q = re.sub(
            r"\b(?:on|from|at)\s+(?:jumia|konga|slot|jiji|temu)\b",
            "", q, flags=re.IGNORECASE,
        )

        # Strip leading qualifiers that poison search
        q = re.sub(
            r"^\s*(?:best|top|good|great|quality|affordable|cheap|premium|original|genuine)\s+",
            "", q, flags=re.IGNORECASE,
        )

        return re.sub(r"\s+", " ", q).strip(" -,.") or raw


# ══════════════════════════════════════════════════════════════
# LLM intent parser
# ══════════════════════════════════════════════════════════════

_LLM_SYSTEM = """You are an intent extraction engine for a Nigerian shopping assistant.
Output ONLY valid JSON. No explanation. No markdown. No extra text.

Extract the user's shopping intent into this exact schema:
{
  "clean_query": "the actual product to search for (concise, 1-5 words)",
  "category": "product category or null",
  "price_min": null or number in NGN,
  "price_max": null or number in NGN,
  "comparison_mode": "best_value" | "cheapest" | "highest_rated" | "newest",
  "store_filter": []
}

Rules:
- clean_query must be the actual searchable product name, not the user's full sentence
- "something to monitor my house that works with Siri" → clean_query: "HomeKit security camera"
- "adapter" → clean_query: "USB adapter" (be slightly more specific when obvious)
- "toilet set under 2 million but more than 900000" → price_min: 900000, price_max: 2000000
- All prices in full NGN numbers (no k/m shorthand)
- If you're unsure about a field, use null"""

_LLM_USER = "Extract shopping intent from: {query}"


class _LLMIntentParser:

    def __init__(self):
        self._available = bool(config.HF_API_TOKEN)

    def parse(self, raw: str, rule_result: SearchIntent) -> SearchIntent:
        """
        Call Qwen to extract structured intent.
        Falls back to rule_result if LLM fails or is unavailable.
        """
        if not self._available:
            logger.warning("[Intent/LLM] No HF_API_TOKEN — using rule result as-is")
            return rule_result

        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM},
                    {"role": "user", "content": _LLM_USER.format(query=raw)},
                ],
                "max_tokens": 200,
                "temperature": 0.1,   # low temp for deterministic JSON
                "stream": False,
            }
            resp = requests.post(
                "https://api-inference.huggingface.co/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.HF_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()

            # Strip markdown fences if model adds them
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

            data = json.loads(text)
            return self._to_intent(raw, data, rule_result)

        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[Intent/LLM] Failed ({e}) — falling back to rules")
            return rule_result

    def _to_intent(self, raw: str, data: dict, fallback: SearchIntent) -> SearchIntent:
        """Map LLM JSON → SearchIntent, filling gaps from rule fallback."""

        def safe_float(v) -> Optional[float]:
            try:
                return float(v) if v else None
            except (TypeError, ValueError):
                return None

        clean = (data.get("clean_query") or "").strip() or fallback.clean_query
        price_max = safe_float(data.get("price_max")) or fallback.budget_max_ngn
        price_min = safe_float(data.get("price_min")) or fallback.budget_min_ngn
        category  = data.get("category") or fallback.category
        mode      = data.get("comparison_mode") or fallback.comparison_mode
        stores    = data.get("store_filter") or fallback.store_filter

        # Validate mode
        valid_modes = {"best_value", "cheapest", "highest_rated", "newest"}
        if mode not in valid_modes:
            mode = "best_value"

        return SearchIntent(
            raw_query=raw,
            clean_query=clean,
            budget_max_ngn=price_max,
            budget_min_ngn=price_min,
            category=category,
            comparison_mode=mode,
            store_filter=stores or [],
            parsed_by="llm",
            confidence=1.0,
        )


# ══════════════════════════════════════════════════════════════
# Public parser — orchestrates both stages
# ══════════════════════════════════════════════════════════════

LLM_CONFIDENCE_THRESHOLD = 0.45   # below this → call LLM


class IntentParser:
    """
    Drop-in replacement for the old IntentParser.
    Same interface: parser.parse(text) → SearchIntent.
    Internally runs rules first, LLM only when confidence is low.
    """

    def __init__(self):
        self._rules = _RuleParser()
        self._llm   = _LLMIntentParser()

    def parse(self, user_text: str) -> SearchIntent:
        # Stage 1: rules (always)
        rule_result = self._rules.parse(user_text)

        logger.info(
            f"[Intent/Rules] {rule_result} "
            f"(conf={rule_result.confidence})"
        )

        # Stage 2: LLM (only when unsure)
        if rule_result.confidence < LLM_CONFIDENCE_THRESHOLD:
            logger.info(
                f"[Intent] Low confidence ({rule_result.confidence}) "
                f"→ calling LLM"
            )
            final = self._llm.parse(user_text, rule_result)
            logger.info(f"[Intent/LLM] {final}")
            return final

        return rule_result