"""
LLM Synthesizer — Qwen/Qwen2.5-7B-Instruct via HuggingFace Inference API.

Responsibilities:
  1. Turn ranked ProductGroups into a natural recommendation
  2. Product disambiguation (uncertain dedup cases)
  3. Fallback to template string if LLM is unavailable/too slow

HuggingFace endpoint used:
  POST https://api-inference.huggingface.co/v1/chat/completions
  Model: Qwen/Qwen2.5-7B-Instruct
"""

import json
import logging
import textwrap
import time
from typing import Optional

import requests

from normalizer.dedup import ProductGroup, SourceEntry
from utils.currency import format_ngn
import config

logger = logging.getLogger(__name__)

HF_ENDPOINT = "https://router.huggingface.co/v1/chat/completions"

# ── Prompts ───────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""
    You are Sift, a Nigerian shopping assistant. You help people find the
    best deals across Jumia, Konga, Slot, Jiji, and Temu.

    Rules:
    - Be concise and practical. Nigerian shoppers value directness.
    - Always mention price in ₦ (Naira).
    - If multiple stores sell the same product, highlight the cheapest.
    - Note condition (new/used) for Jiji listings.
    - Give a clear winner recommendation and explain why briefly.
    - Use simple formatting: numbered lists, no markdown headers.
    - Max 250 words. Be punchy.
""").strip()

RECOMMENDATION_TEMPLATE = textwrap.dedent("""
    User searched for: {query}
    Budget: {budget}

    Top products found across Nigerian stores:

    {products_text}

    Write a concise recommendation:
    1. Name the top pick and why it's best value
    2. Mention the budget pick if different from top pick
    3. Any caution (Jiji used items, price-to-quality concerns)
    Keep it under 200 words. No markdown, plain text.
""").strip()

DISAMBIGUATION_TEMPLATE = textwrap.dedent("""
    Are these two product listings the same physical product?

    Product A: {title_a}
    Product B: {title_b}

    Answer only: YES or NO (then one sentence reason).
""").strip()


def _products_to_text(groups: list[ProductGroup], max_items: int = 5) -> str:
    """Format groups as text for the LLM prompt."""
    lines = []
    for i, g in enumerate(groups[:max_items], 1):
        # Price across stores
        store_prices = ", ".join(
            f"{s.store.capitalize()}: {s.price_display}"
            for s in sorted(g.sources, key=lambda s: s.price_ngn or float("inf"))
        )
        rating_str = f"⭐ {g.avg_rating}" if g.avg_rating else ""
        reviews_str = f"({g.total_reviews} reviews)" if g.total_reviews else ""
        cond_str = f"[{g.condition}]" if g.condition and g.condition != "unknown" else ""

        lines.append(
            f"{i}. {g.canonical_title} {cond_str}\n"
            f"   Prices: {store_prices}\n"
            f"   {rating_str} {reviews_str}".strip()
        )
    return "\n\n".join(lines)


class LLMSynthesizer:

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token or config.HF_API_TOKEN
        self._available = bool(self.api_token)
        if not self._available:
            logger.warning(
                "[LLM] No HF_API_TOKEN — will use template fallback. "
                "Add HF_API_TOKEN to .env for AI recommendations."
            )

    # ── Main synthesis ─────────────────────────────────────────

    def synthesize(
        self,
        groups: list[ProductGroup],
        query: str,
        budget_ngn: Optional[float] = None,
    ) -> str:
        """
        Generate a natural recommendation from ranked ProductGroups.
        Returns plain text suitable for a Telegram message.
        """
        if not groups:
            return "Sorry, I couldn't find any matching products. Try a different search."

        if self._available:
            try:
                return self._llm_recommendation(groups, query, budget_ngn)
            except Exception as e:
                logger.error(f"[LLM] API error: {e} — falling back to template")

        return self._template_recommendation(groups, query, budget_ngn)

    def check_same_product(self, title_a: str, title_b: str) -> Optional[bool]:
        """
        Ask LLM: are these the same product?
        Returns True/False/None (None = couldn't determine).
        Used for 60-82% similarity cases in dedup.
        """
        if not self._available:
            return None

        prompt = DISAMBIGUATION_TEMPLATE.format(title_a=title_a, title_b=title_b)
        try:
            response = self._call_api(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=60,
                temperature=0.1,
            )
            answer = response.strip().upper()
            if answer.startswith("YES"):
                return True
            if answer.startswith("NO"):
                return False
            return None
        except Exception as e:
            logger.error(f"[LLM] Disambiguation error: {e}")
            return None

    # ── LLM call ──────────────────────────────────────────────

    def _llm_recommendation(
        self,
        groups: list[ProductGroup],
        query: str,
        budget_ngn: Optional[float],
    ) -> str:
        budget_str = format_ngn(budget_ngn) if budget_ngn else "Not specified"
        products_text = _products_to_text(groups, max_items=5)

        user_msg = RECOMMENDATION_TEMPLATE.format(
            query=query,
            budget=budget_str,
            products_text=products_text,
        )

        response = self._call_api(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=config.LLM_MAX_TOKENS,
            temperature=config.LLM_TEMPERATURE,
        )
        return response.strip()

    def _call_api(
        self,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.7,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": config.LLM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        resp = requests.post(
            HF_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=config.LLM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # OpenAI-compatible response format
        return data["choices"][0]["message"]["content"]

    # ── Template fallback ─────────────────────────────────────

    def _template_recommendation(
        self,
        groups: list[ProductGroup],
        query: str,
        budget_ngn: Optional[float],
    ) -> str:
        """
        Deterministic template when LLM is unavailable.
        Still useful — shows top pick and price comparison.
        """
        if not groups:
            return "No matching products found."

        top = groups[0]
        lines = []

        # Top pick summary
        top_price = format_ngn(top.lowest_price)
        top_store = (top.best_store or "").capitalize()
        rating_str = f" · ⭐ {top.avg_rating}" if top.avg_rating else ""
        lines.append(
            f"🏆 Top pick: {top.canonical_title}\n"
            f"   Best price: {top_price} on {top_store}{rating_str}"
        )

        # Price comparison across stores (for top pick)
        if top.source_count > 1:
            sorted_sources = sorted(
                top.sources, key=lambda s: s.price_ngn or float("inf")
            )
            store_lines = [
                f"   • {s.store.capitalize()}: {s.price_display}"
                for s in sorted_sources
            ]
            lines.append("   Prices across stores:\n" + "\n".join(store_lines))

        # Budget pick (if top isn't already cheapest)
        if budget_ngn and len(groups) > 1:
            budget_pick = min(
                [g for g in groups if g.lowest_price and g.lowest_price <= budget_ngn],
                key=lambda g: g.lowest_price or float("inf"),
                default=None,
            )
            if budget_pick and budget_pick.id != top.id:
                lines.append(
                    f"\n💰 Budget pick: {budget_pick.canonical_title}\n"
                    f"   {format_ngn(budget_pick.lowest_price)} on "
                    f"{(budget_pick.best_store or '').capitalize()}"
                )

        lines.append(
            "\n💡 Tip: Add HF_API_TOKEN to .env for AI-powered analysis."
        )
        return "\n".join(lines)


# ── Singleton ──────────────────────────────────────────────────
_synthesizer: Optional[LLMSynthesizer] = None


def get_synthesizer() -> LLMSynthesizer:
    global _synthesizer
    if _synthesizer is None:
        _synthesizer = LLMSynthesizer()
    return _synthesizer
