
import re
import json
import hashlib
import logging
import requests
from dataclasses import dataclass, field
from typing import Optional

from utils.currency import extract_budget
import config

logger = logging.getLogger(__name__)


# ── Category keyword table ────────────────────────────────────
_CATEGORIES = {
    "phone":       ["phone", "iphone", "samsung", "tecno", "infinix", "smartphone",
                    "android", "itel", "xiaomi", "redmi", "oppo", "pixel", "galaxy"],
    "laptop":      ["laptop", "notebook", "macbook", "chromebook", "thinkpad"],
    "tablet":      ["tablet", "ipad", "tab", "surface pro"],
    "tv":          ["tv", "television", "smart tv", "led tv", "oled"],
    "blender":     ["blender", "mixer", "food processor", "juicer"],
    "fridge":      ["fridge", "refrigerator", "freezer", "cooler"],
    "ac":          ["air conditioner", "air con", "ac unit", "split unit"],
    "generator":   ["generator", "gen set", "inverter"],
    "headphone":   ["headphone", "earphone", "earbud", "airpod", "headset", "tws"],
    "speaker":     ["speaker", "soundbar", "bluetooth speaker"],
    "camera":      ["camera", "dslr", "mirrorless", "webcam", "cctv"],
    "watch":       ["smartwatch", "watch", "wristwatch"],
    "shoe":        ["shoe", "sneaker", "sandal", "boot"],
    "clothing":    ["shirt", "dress", "trouser", "skirt", "jean", "cloth"],
    "perfume":     ["perfume", "cologne", "fragrance"],
    "baby":        ["baby", "diaper", "nappy", "pram", "stroller", "formula"],
    "kitchen":     ["pot", "pan", "cookware", "kettle", "rice cooker", "microwave", "toaster"],
    "power_bank":  ["power bank", "powerbank", "portable charger"],
    "charger":     ["charger", "adapter", "cable", "usb hub"],
    "gaming":      ["console", "ps5", "playstation", "xbox", "nintendo", "gaming"],
    "ups":         ["ups", "power backup", "uninterruptible power supply"],
    "ring_light":  ["ring light", "ring lamp", "led ring", "studio light", "fill light"],
    "furniture":   ["sofa", "chair", "table", "bed", "wardrobe", "shelf", "furniture"],
    "mattress":    ["mattress", "foam", "spring bed"],
}

_COMPARISON_MODES = {
    "cheapest":      ["cheapest", "lowest price", "most affordable"],
    "best_value":    ["best value", "best deal", "worth", "value for money"],
    "highest_rated": ["best rated", "highest rated", "top rated", "best quality"],
    "newest":        ["latest", "newest", "most recent", "new release"],
}

# Cost model
AMBIGUITY_THRESHOLD = 0.65   # above this → ask a question
SEARCH_COST_SEC     = 60     # average search takes ~60 seconds
QUESTION_COST_SEC   = 5      # asking a question costs ~5 seconds of user effort


# ══════════════════════════════════════════════════════════════
# Intent data model
# ══════════════════════════════════════════════════════════════

@dataclass
class ParsedIntent:
    """
    Rich intent produced by the intent layer.

    canonical_search_term:  clean keyword string sent to scrapers
    candidate_terms:        alternative search strings (tried if canonical returns few results)
    confidence:             0-1, how sure we are about the product type
    ambiguity:              0-1, how unclear the specific variant is
    needs_clarification:    True → ask a question before searching
    clarification_question: the question to ask
    """
    raw_query: str
    canonical_search_term: str

    candidate_terms: list = field(default_factory=list)

    confidence: float = 1.0
    ambiguity: float = 0.0
    needs_clarification: bool = False
    clarification_question: Optional[str] = None

    budget_max_ngn: Optional[float] = None
    budget_min_ngn: Optional[float] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    store_filter: list = field(default_factory=list)
    comparison_mode: str = "best_value"
    parsed_by: str = "rules"

    @property
    def budget_ngn(self) -> Optional[float]:
        return self.budget_max_ngn

    @property
    def clean_query(self) -> str:
        """Backward-compat alias used by pipeline and normalizer."""
        return self.canonical_search_term

    def cache_key(self) -> str:
        """
        Stable hash of everything that affects results.
        "laptop" with budget=300k hashes differently from "laptop" with no budget.
        Prevents wrong cached results being served for different intents.
        """
        key_dict = {
            "query":      self.canonical_search_term.lower().strip(),
            "budget_max": self.budget_max_ngn,
            "budget_min": self.budget_min_ngn,
            "brand":      (self.brand or "").lower().strip(),
            "stores":     sorted(self.store_filter),
            "mode":       self.comparison_mode,
        }
        raw = json.dumps(key_dict, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def should_search(self) -> bool:
        """
        Cost model: search immediately unless ambiguity is high enough
        that a question would save more time than it costs.

        Expected saving = search_cost * ambiguity = 60 * 0.74 = ~44s
        Question cost   = 5s
        → 44 > 5 → ask

        Expected saving = 60 * 0.30 = 18s vs 5s → borderline, but search anyway
        """
        if not self.needs_clarification:
            return True
        expected_saving = SEARCH_COST_SEC * self.ambiguity
        return expected_saving <= QUESTION_COST_SEC

    def __str__(self):
        parts = [f"term='{self.canonical_search_term}'"]
        if self.budget_max_ngn:
            parts.append(f"max=₦{self.budget_max_ngn:,.0f}")
        if self.category:
            parts.append(f"cat={self.category}")
        parts.append(f"conf={self.confidence:.2f} amb={self.ambiguity:.2f}")
        parts.append(f"by={self.parsed_by}")
        if self.needs_clarification:
            parts.append("→ ASK")
        return f"Intent({', '.join(parts)})"


# Backward-compat alias
SearchIntent = ParsedIntent


# ══════════════════════════════════════════════════════════════
# Rule-based fast path
# ══════════════════════════════════════════════════════════════

def _extract_min_budget(q: str) -> Optional[float]:
    patterns = [
        r"(?:more\s+than|above|over|at\s+least|minimum|min)\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
        r"(?:from|starting\s+(?:from|at))\s*[₦#\$]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
    ]
    for pattern in patterns:
        m = re.search(pattern, q, re.IGNORECASE)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
                mult   = (m.group(2) or "").lower()
                if mult in ("k", "thousand"):  amount *= 1_000
                elif mult in ("m", "million"): amount *= 1_000_000
                if amount > 0:
                    return amount
            except ValueError:
                continue
    return None


def _detect_category(q: str) -> Optional[str]:
    normalized = re.sub(r'[-–—/]', ' ', q.lower())
    for cat, keywords in _CATEGORIES.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", normalized):
                return cat
    return None


def _detect_mode(q: str) -> str:
    for mode, keywords in _COMPARISON_MODES.items():
        for kw in keywords:
            if kw in q:
                return mode
    return "best_value"


def _detect_stores(q: str) -> list:
    return [s for s in ["jumia", "konga", "slot", "jiji", "temu"] if s in q.lower()]


def _is_clean_structured(raw: str) -> bool:
    """
    True if query is already keyword-style and rules can handle it reliably.
    Anything conversational, metaphorical, or descriptive goes to LLM.
    """
    q = raw.lower().strip()

    conversational_patterns = [
        r"\bi\s+(need|want|am\s+looking|would\s+like|require)\b",
        r"\b(find|show|get|help|suggest|recommend)\s+(me|us)\b",
        r"\blooking\s+for\b",
        r"\bsomething\s+(that|to|which|for|with)\b",
        r"\bworks\s+with\b",
        r"\bcompatible\s+with\b",
        r"\bwhen\s+nepa\b",
        r"\bto\s+power\b",
        r"\bfor\s+(university|school|work|office|gaming|streaming|content\s+creat)\b",
        r":-",
        r"\bunder\s+\$",
        r"\bround\s+thing\b",
        r"\bthing\s+(that|to|which|for)\b",
        r"\bpeople\s+use\b",
        r"\bcreators?\s+use\b",
    ]
    for pattern in conversational_patterns:
        if re.search(pattern, q, re.IGNORECASE | re.MULTILINE):
            return False

    # Long queries are likely conversational
    if len(raw.split()) > 7:
        return False

    return True


def _strip_to_keywords(raw: str) -> str:
    """Remove budget, qualifiers, store names from a clean query."""
    q = raw
    for pattern in [
        r"\b(?:under|below|less\s+than|max|maximum)\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
        r"\bbudget\s*(?:of|:)?\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
        r"[₦#]\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?",
        r"\bunder\s+\$[\d,]+\.?\d*",
        r"\b(?:more\s+than|above|over|at\s+least)\s*[₦#\$]?\s*[\d,]+\.?\d*\s*(?:k|m|thousand|million)?\b",
        r"\b(?:best\s+value|best\s+deal|cheapest|most\s+affordable|best\s+rated|top\s+rated|latest|newest)\b",
        r"\b(?:on|from|at)\s+(?:jumia|konga|slot|jiji|temu)\b",
        r"^\s*(?:best|top|good|great|quality|affordable|cheap|premium)\s+",
    ]:
        q = re.sub(pattern, "", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip(" -,.") or raw


def _rules_parse(raw: str) -> ParsedIntent:
    q          = raw.lower()
    budget_max = extract_budget(q)
    budget_min = _extract_min_budget(q)
    category   = _detect_category(q)
    mode       = _detect_mode(q)
    stores     = _detect_stores(q)
    clean      = _strip_to_keywords(raw)

    confidence = 0.85 if category else 0.60
    ambiguity  = 0.10 if category else 0.40

    return ParsedIntent(
        raw_query=raw,
        canonical_search_term=clean,
        candidate_terms=[clean],
        confidence=confidence,
        ambiguity=ambiguity,
        needs_clarification=False,
        budget_max_ngn=budget_max,
        budget_min_ngn=budget_min,
        category=category,
        store_filter=stores,
        comparison_mode=mode,
        parsed_by="rules",
    )


# ══════════════════════════════════════════════════════════════
# LLM Intent Layer
# ══════════════════════════════════════════════════════════════

_LLM_SYSTEM = """You are an e-commerce search intent parser for a Nigerian shopping assistant called Sift.

Convert user requests into structured product search intent.
Return ONLY valid JSON. No markdown, no explanation, no extra text.

Schema:
{
  "canonical_search_term": "2-5 word product keyword string",
  "candidate_terms": ["primary term", "variant 1", "variant 2"],
  "confidence": 0.0-1.0,
  "ambiguity": 0.0-1.0,
  "needs_clarification": true/false,
  "clarification_question": "one specific question or null",
  "category": "product category or null",
  "budget_max_ngn": null or number,
  "budget_min_ngn": null or number,
  "brand": "brand name or null",
  "comparison_mode": "best_value" | "cheapest" | "highest_rated" | "newest"
}

Rules for canonical_search_term:
  MUST be keywords only — what a human types in a search bar.
  Strip ALL intent language: "I need", "find me", "looking for", "something that", "I want"
  Strip ALL vague qualifiers: "affordable", "decent", "good for university"
  Strip availability phrases: "available in Nigeria"
  Keep: brand name, product type, key specs (storage, RAM, screen size)

Rules for candidate_terms:
  2-3 alternative keyword strings for the same product.
  "ring light" → ["ring light", "LED ring light", "selfie ring light"]
  "iPhone charger" → ["iPhone charger", "Apple USB-C charger", "Lightning cable"]

Rules for confidence (how sure are you about the product type):
  0.9+ obvious ("Samsung A15")
  0.7  clear but generic ("laptop")
  0.5  somewhat clear ("something for lighting")
  0.3  unclear ("something round content creators use")

Rules for ambiguity (how unclear is the specific variant):
  0.1  very specific ("Samsung Galaxy A15 128GB")
  0.4  some ambiguity ("iPhone charger" — which iPhone?)
  0.7  high ambiguity ("phone under 200k" — which phone?)
  0.9  very ambiguous ("I need a phone")

needs_clarification: TRUE only if ambiguity >= 0.65 AND one question would significantly
  improve results. Never ask obvious questions.
  BAD: "What product are you looking for?" (too vague)
  GOOD: "Which Apple device are you charging — iPhone, iPad, or MacBook?" (specific)

clarification_question: one focused question or null. Must be answerable in one sentence.

Budget conversion: 1 USD = 1620 NGN, 1 EUR = 1750 NGN. Convert and store in NGN.

Nigerian context:
  "NEPA" = electricity blackout → needs UPS or generator
  "tokunbo" = used/fairly-used condition
  "Aba-made" = locally manufactured

Examples:

Input: "round thing content creators use for lighting"
Output: {"canonical_search_term":"ring light","candidate_terms":["ring light","LED ring light","selfie ring light"],"confidence":0.91,"ambiguity":0.12,"needs_clarification":false,"clarification_question":null,"category":"ring_light","budget_max_ngn":null,"budget_min_ngn":null,"brand":null,"comparison_mode":"best_value"}

Input: "I need something to power my laptop when NEPA takes light"
Output: {"canonical_search_term":"UPS","candidate_terms":["UPS","laptop UPS","uninterruptible power supply"],"confidence":0.63,"ambiguity":0.74,"needs_clarification":true,"clarification_question":"Are you looking for a UPS just for a laptop, or a larger backup for your home?","category":"ups","budget_max_ngn":null,"budget_min_ngn":null,"brand":null,"comparison_mode":"best_value"}

Input: "Apple charger"
Output: {"canonical_search_term":"iPhone charger","candidate_terms":["iPhone charger","Apple USB-C charger","Lightning charger"],"confidence":0.68,"ambiguity":0.59,"needs_clarification":true,"clarification_question":"Which Apple device are you charging — iPhone, iPad, or MacBook?","category":"charger","budget_max_ngn":null,"budget_min_ngn":null,"brand":"Apple","comparison_mode":"best_value"}

Input: "I need a phone"
Output: {"canonical_search_term":"smartphone","candidate_terms":["smartphone","Android phone","mobile phone"],"confidence":0.34,"ambiguity":0.89,"needs_clarification":true,"clarification_question":"What is your budget for the phone?","category":"phone","budget_max_ngn":null,"budget_min_ngn":null,"brand":null,"comparison_mode":"best_value"}

Input: "Dell laptop under 110k"
Output: {"canonical_search_term":"Dell laptop","candidate_terms":["Dell laptop","Dell notebook","Dell computer"],"confidence":0.95,"ambiguity":0.15,"needs_clarification":false,"clarification_question":null,"category":"laptop","budget_max_ngn":110000,"budget_min_ngn":null,"brand":"Dell","comparison_mode":"best_value"}"""


class LLMIntentParser:

    def __init__(self):
        self._available = bool(config.HF_API_TOKEN)
        if not self._available:
            logger.warning("[Intent/LLM] No HF_API_TOKEN — LLM intent parsing disabled")

    def parse(self, raw: str, fallback: Optional[ParsedIntent] = None) -> Optional[ParsedIntent]:
        """Call Qwen. Returns None on failure — caller falls back to rules."""
        if not self._available:
            return None

        try:
            payload = {
                "model": config.LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM},
                    {"role": "user",   "content": f'Parse this shopping request: "{raw}"'},
                ],
                "max_tokens": 350,
                "temperature": 0.15,
                "stream": False,
            }
            resp = requests.post(
                "https://router.huggingface.co/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.HF_API_TOKEN}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
            data = json.loads(text)
            return self._build(raw, data, fallback)

        except Exception as e:
            logger.warning(f"[Intent/LLM] Failed: {e}")
            return None

    def _build(self, raw: str, data: dict, fallback: Optional[ParsedIntent]) -> ParsedIntent:
        def sf(v):
            try:    return float(v) if v is not None else None
            except: return None
        def sl(v):
            if isinstance(v, list): return [str(x) for x in v if x]
            return []

        canonical  = (data.get("canonical_search_term") or "").strip()
        if not canonical:
            canonical = fallback.canonical_search_term if fallback else raw

        candidates = sl(data.get("candidate_terms")) or [canonical]
        confidence = max(0.0, min(1.0, sf(data.get("confidence")) or 0.5))
        ambiguity  = max(0.0, min(1.0, sf(data.get("ambiguity"))  or 0.5))

        needs_q = bool(data.get("needs_clarification", False))
        q_text  = data.get("clarification_question") or None

        # Apply cost model: suppress question if ambiguity below threshold
        if ambiguity < AMBIGUITY_THRESHOLD:
            needs_q = False
            q_text  = None

        budget_max = sf(data.get("budget_max_ngn")) or (fallback.budget_max_ngn if fallback else None)
        budget_min = sf(data.get("budget_min_ngn")) or (fallback.budget_min_ngn if fallback else None)

        # Safety net: also try extracting budget from raw text
        if not budget_max:
            budget_max = extract_budget(raw.lower())
        if not budget_min:
            budget_min = _extract_min_budget(raw.lower())

        mode = data.get("comparison_mode") or "best_value"
        if mode not in {"best_value", "cheapest", "highest_rated", "newest"}:
            mode = "best_value"

        return ParsedIntent(
            raw_query=raw,
            canonical_search_term=canonical,
            candidate_terms=candidates,
            confidence=confidence,
            ambiguity=ambiguity,
            needs_clarification=needs_q,
            clarification_question=q_text,
            budget_max_ngn=budget_max,
            budget_min_ngn=budget_min,
            category=data.get("category") or (fallback.category if fallback else None),
            brand=data.get("brand") or None,
            store_filter=sl(data.get("store_filter")) or (fallback.store_filter if fallback else []),
            comparison_mode=mode,
            parsed_by="llm",
        )


# ══════════════════════════════════════════════════════════════
# Public IntentParser
# ══════════════════════════════════════════════════════════════

class IntentParser:
    """
    Two-stage intent parser.

    Stage 1 (rules):  fast, free, handles clean keyword queries
    Stage 2 (LLM):    handles conversational, metaphorical, ambiguous queries

    LLM is only called when:
      - query contains conversational language, OR
      - rule confidence < 0.6

    The returned ParsedIntent carries:
      - canonical_search_term  → what to send to scrapers
      - candidate_terms        → fallback search strings
      - needs_clarification    → True if bot should ask before searching
      - clarification_question → exact question to ask the user
      - cache_key()            → stable hash for caching
      - should_search()        → True if search is better than asking
    """

    def __init__(self):
        self._llm = LLMIntentParser()

    def parse(self, user_text: str) -> ParsedIntent:
        raw         = user_text.strip()
        rule_result = _rules_parse(raw)

        logger.info(f"[Intent/Rules] {rule_result}")

        needs_llm = (
            not _is_clean_structured(raw)
            or rule_result.confidence < 0.6
        )

        if needs_llm:
            reason = "conversational" if not _is_clean_structured(raw) else f"low-conf({rule_result.confidence})"
            logger.info(f"[Intent] → LLM ({reason})")
            llm_result = self._llm.parse(raw, fallback=rule_result)
            if llm_result:
                logger.info(f"[Intent/LLM] {llm_result}")
                return llm_result
            logger.info("[Intent/LLM] unavailable — using rules")

        return rule_result