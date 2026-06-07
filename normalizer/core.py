"""
Normalizer — orchestrates dedup + scoring.

Takes raw Product list from all scrapers.
Returns ranked ProductGroup list ready for LLM synthesis.
"""

import logging
from typing import Optional

from scrapers.base import Product
from normalizer.dedup import Deduplicator, ProductGroup
from normalizer.scorer import Scorer
import config

logger = logging.getLogger(__name__)


class Normalizer:

    def __init__(self):
        self.deduplicator = Deduplicator(
            same_source_threshold=config.DEDUP_SAME_SOURCE_THRESHOLD,
            cross_source_threshold=config.DEDUP_CROSS_SOURCE_THRESHOLD,
        )
        self.scorer = Scorer()

    def normalize(
        self,
        products: list[Product],
        query: str,
        budget_ngn: Optional[float] = None,
        budget_min_ngn: Optional[float] = None,
        top_n: int = 10,
    ) -> list[ProductGroup]:
        """
        Full normalize pipeline:
          1. Filter garbage
          2. Deduplicate (within-source + cross-source grouping)
          3. Score each group
          4. Sort by score, return top N
        """
        # Step 1: Filter
        cleaned = self._filter(products)
        logger.info(f"[Normalizer] After filter: {len(cleaned)}/{len(products)}")

        # Step 2: Deduplicate + group
        groups = self.deduplicator.deduplicate(cleaned)
        if budget_ngn:
            before = len(groups)
            groups = self._filter_budget(groups, budget_max=budget_ngn, budget_min=budget_min_ngn)
            logger.info(f"[Normalizer] Budget filter: {before} → {len(groups)}")

            # Step 4: Hard relevance filter — remove products with zero keyword overlap
        groups = self._filter_relevance(groups, query)
        logger.info(f"[Normalizer] After relevance filter: {len(groups)}")

        # Step 3: Score
        for g in groups:
            g.score = self.scorer.score(g, query, budget_ngn)

        # Step 4: Sort + top N
        groups.sort(key=lambda g: g.score, reverse=True)
        result = groups[:top_n]

        logger.info(f"[Normalizer] Final: {len(result)} product groups (from {len(groups)})")
        return result

    def _filter(self, products: list[Product]) -> list[Product]:
        """Remove clearly bad entries."""
        good = []
        for p in products:
            if not p.title or p.title.strip() in ("", "Unknown", "N/A"):
                continue
            if not p.url or not p.url.startswith("http"):
                continue
            if len(p.title) < 5:
                continue
            good.append(p)
        return good

    def _filter_budget(
        self,
        groups: list,
        budget_max: Optional[float] = None,
        budget_min: Optional[float] = None,
        tolerance: float = 0.05,
    ) -> list:
        result = groups
        if budget_max:
            ceiling = budget_max * (1 + tolerance)
            result = [g for g in result if g.lowest_price is None or g.lowest_price <= ceiling]
        if budget_min:
            floor = budget_min * (1 - tolerance)   # 5% tolerance below min too
            result = [g for g in result if g.lowest_price is None or g.lowest_price >= floor]
        return result
    def _filter_relevance(
        self,
        groups: list[ProductGroup],
        query: str,
    ) -> list[ProductGroup]:
        """
        Remove products with zero overlap with the search query.
        'blender' query → 'Kitchen Tap' is filtered out.
        Uses cleaned query words (2+ chars, ignoring stopwords).
        """
        from normalizer.dedup import normalize_title, _STOPWORDS
        norm_query = normalize_title(query)
        query_words = [w for w in norm_query.split() if len(w) >= 3 and w not in _STOPWORDS]

        if not query_words:
            return groups  # can't filter without signal

        result = []
        for g in groups:
            title_lower = normalize_title(g.canonical_title)
            # At least one query word must appear in the title
            if any(qw in title_lower for qw in query_words):
                result.append(g)
            else:
                logger.debug(f"[Relevance] Dropped: '{g.canonical_title[:50]}'")
        return result
__all__ = ["Normalizer"]
