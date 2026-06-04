"""
Scorer — assigns a 0-100 quality score to each ProductGroup.

Score formula (Phase 1):
  Title relevance to query:  35 pts
  Budget fit:                25 pts  (0 if no budget given)
  Rating quality:            20 pts
  Review count / popularity:  10 pts
  Availability:               5 pts
  Multi-store presence:       5 pts  (bonus for found on 2+ stores)

Phase 3 will add:
  Seller trust score:        +10 pts
  Price vs 30-day average:   +10 pts
"""

import math
import re
import logging
from typing import Optional

from normalizer.dedup import ProductGroup, normalize_title

logger = logging.getLogger(__name__)


class Scorer:

    def score(
        self,
        group: ProductGroup,
        query: str,
        budget_ngn: Optional[float] = None,
    ) -> float:
        score = 0.0
        norm_query = normalize_title(query)

        # ── 1. Title relevance (35 pts) ───────────────────────
        score += self._relevance_score(group.canonical_title, norm_query, max_pts=35)

        # ── 2. Budget fit (25 pts) ────────────────────────────
        score += self._budget_score(group.lowest_price, budget_ngn, max_pts=25)

        # ── 3. Rating (20 pts) ───────────────────────────────
        score += self._rating_score(group.avg_rating, max_pts=20)

        # ── 4. Popularity (10 pts) ───────────────────────────
        score += self._popularity_score(group.total_reviews, max_pts=10)

        # ── 5. Availability (5 pts) ──────────────────────────
        if any(s.availability == "in_stock" for s in group.sources):
            score += 5

        # ── 6. Multi-store bonus (5 pts) ─────────────────────
        if group.source_count >= 2:
            score += min(group.source_count * 1.5, 5)

        return round(max(0.0, min(100.0, score)), 2)

    # ── Subscores ─────────────────────────────────────────────

    def _relevance_score(self, title: str, norm_query: str, max_pts: float) -> float:
        """
        Word overlap + ngram-level match.
        Better than simple word overlap for partial matches.
        """
        query_words = set(norm_query.split())
        title_words = set(normalize_title(title).split())

        if not query_words:
            return max_pts * 0.5  # neutral if no query

        # Word overlap
        overlap = len(query_words & title_words) / len(query_words)

        # Bonus: query appears as substring in normalized title
        norm_title = normalize_title(title)
        substring_bonus = 0.0
        if norm_query in norm_title:
            substring_bonus = 0.2

        relevance = min(1.0, overlap + substring_bonus)
        return relevance * max_pts

    def _budget_score(
        self,
        price: Optional[float],
        budget: Optional[float],
        max_pts: float,
    ) -> float:
        if not budget:
            return max_pts * 0.4 if price else 0  # neutral when no budget

        if not price:
            return 0

        if price <= budget:
            # Under budget — more points for being well under
            ratio = price / budget   # 0.5 = half price, 1.0 = at budget
            # Sweet spot: 70-90% of budget → full points
            # Very cheap might be lower quality
            if ratio < 0.3:
                return max_pts * 0.6    # suspiciously cheap
            elif ratio <= 0.9:
                return max_pts * (0.7 + 0.3 * (ratio / 0.9))
            else:
                return max_pts          # at budget
        else:
            # Over budget — penalize proportionally
            over_pct = (price - budget) / budget
            penalty = min(over_pct * max_pts, max_pts)
            return -penalty

    def _rating_score(self, rating: Optional[float], max_pts: float) -> float:
        if not rating:
            return max_pts * 0.3  # neutral when unknown
        # 5.0 → max, 3.0 → 60%, <2.0 → 0
        normalized = max(0, (rating - 2.0) / 3.0)   # 2.0-5.0 → 0-1
        return normalized * max_pts

    def _popularity_score(self, review_count: int, max_pts: float) -> float:
        if not review_count:
            return 0
        # Log scale: 1 review → ~0 pts, 1000 reviews → max pts
        return min(math.log10(review_count + 1) / 3.0, 1.0) * max_pts
