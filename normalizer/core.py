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


__all__ = ["Normalizer"]
