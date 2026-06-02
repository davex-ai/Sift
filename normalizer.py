"""
Data Normalizer — takes raw Product lists from multiple scrapers,
deduplicates, scores, and returns a clean ranked list.

This is where multi-source results become a single coherent response.
"""

import re
from dataclasses import dataclass
from typing import Optional
from difflib import SequenceMatcher

from ..scrapers.base import Product
from ..utils.currency import format_ngn


@dataclass
class NormalizedResult:
    """Final product ready for AI synthesis / display."""
    rank: int
    title: str
    price_ngn: Optional[float]
    price_display: str          # "₦450,000" or "$299 (~₦478,400)"
    url: str
    image_url: Optional[str]
    rating: Optional[float]
    review_count: Optional[int]
    availability: str
    source: str
    delivery_info: Optional[str]
    score: float                # Relevance + quality score (0–100)
    extra: dict


class Normalizer:
    """
    Takes results from N scrapers and produces a clean ranked list.

    Steps:
      1. Filter out garbage (no price, no title)
      2. Deduplicate near-identical products
      3. Score each product (price, rating, relevance)
      4. Sort and return top N
    """

    def normalize(
        self,
        products: list[Product],
        query: str,
        budget_ngn: Optional[float] = None,
        top_n: int = 10,
    ) -> list[NormalizedResult]:

        # Step 1: Basic filtering
        filtered = [p for p in products if p.title and p.title != "Unknown"]

        # Step 2: Deduplicate
        deduped = self._deduplicate(filtered)

        # Step 3: Score
        scored = [
            (p, self._score(p, query, budget_ngn))
            for p in deduped
        ]
        scored.sort(key=lambda x: x[1], reverse=True)

        # Step 4: Build NormalizedResult
        results = []
        for rank, (product, score) in enumerate(scored[:top_n], start=1):
            results.append(NormalizedResult(
                rank=rank,
                title=product.title,
                price_ngn=product.price_ngn,
                price_display=self._price_display(product),
                url=product.url,
                image_url=product.image_url,
                rating=product.rating,
                review_count=product.review_count,
                availability=product.availability,
                source=product.source,
                delivery_info=product.delivery_info,
                score=round(score, 2),
                extra=product.extra,
            ))

        return results

    # -----------------------------------------------------------
    # Deduplication
    # -----------------------------------------------------------

    def _deduplicate(self, products: list[Product]) -> list[Product]:
        """
        Remove near-duplicate products.
        Two products are duplicates if title similarity > 85%
        AND they're from the same source.
        """
        seen: list[Product] = []

        for product in products:
            is_dupe = False
            for existing in seen:
                if (
                    existing.source == product.source
                    and self._similarity(existing.title, product.title) > 0.85
                ):
                    is_dupe = True
                    break
            if not is_dupe:
                seen.append(product)

        return seen

    def _similarity(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    # -----------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------

    def _score(
        self,
        product: Product,
        query: str,
        budget_ngn: Optional[float],
    ) -> float:
        """
        Score 0-100. Higher = better match for user query.
        Weights:
          - Title relevance to query:  35 pts
          - Within budget:             25 pts
          - Rating quality:            20 pts
          - Review count (popularity): 10 pts
          - Availability:              10 pts
        """
        score = 0.0

        # Title relevance
        query_words = set(query.lower().split())
        title_words = set(product.title.lower().split())
        overlap = len(query_words & title_words) / max(len(query_words), 1)
        score += overlap * 35

        # Budget fit
        if budget_ngn and product.price_ngn:
            if product.price_ngn <= budget_ngn:
                # Full points if under budget, more points if well under
                ratio = product.price_ngn / budget_ngn
                score += (1 - ratio * 0.3) * 25  # closer to budget = more relevant
            else:
                # Over budget: penalize proportionally
                over_by = (product.price_ngn - budget_ngn) / budget_ngn
                score -= min(over_by * 20, 20)  # max 20pt penalty
        elif product.price_ngn:
            score += 10  # has a price, that's good

        # Rating
        if product.rating:
            score += (product.rating / 5.0) * 20

        # Popularity (log scale — 1000 reviews ≈ max points)
        if product.review_count:
            import math
            score += min(math.log10(product.review_count + 1) / 3, 1) * 10

        # Availability
        if product.availability == "in_stock":
            score += 10

        return max(0, score)

    # -----------------------------------------------------------
    # Display formatting
    # -----------------------------------------------------------

    def _price_display(self, product: Product) -> str:
        if product.currency == "NGN" or not product.price_ngn:
            return format_ngn(product.price_ngn)

        # Foreign currency: show both
        ngn_str = format_ngn(product.price_ngn)
        return f"{product.price_raw} (~{ngn_str})"


def extract_budget(query: str) -> Optional[float]:
    """
    Parse budget from natural language query.
    Examples:
      "laptop under 500k"  → 500_000
      "phone below ₦200000" → 200_000
      "under $300"         → None (returns USD, caller converts)
      "budget of 1.5m"     → 1_500_000
    """
    query_lower = query.lower()

    # Match patterns like "500k", "1.5m", "200000", "₦500,000"
    patterns = [
        r"(?:under|below|max|budget\s+of|less\s+than)\s*[₦#]?\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
        r"[₦#]\s*([\d,]+\.?\d*)\s*(k|m|thousand|million)?",
    ]

    for pattern in patterns:
        match = re.search(pattern, query_lower)
        if match:
            amount_str = match.group(1).replace(",", "")
            multiplier_str = (match.group(2) or "").lower()
            try:
                amount = float(amount_str)
                if multiplier_str in ("k", "thousand"):
                    amount *= 1_000
                elif multiplier_str in ("m", "million"):
                    amount *= 1_000_000
                return amount
            except ValueError:
                continue

    return None
