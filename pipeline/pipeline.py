"""

Changes from previous version:
  - Cache key now uses intent.cache_key() (hashes query + budget + brand + stores)
    instead of just intent.clean_query — prevents wrong cached results
  - Candidate terms: if canonical returns < 3 results, searches candidate_terms too
  - should_search() check: if LLM says ask, pipeline returns empty + question instead of searching
  - normalizer.normalize() now receives budget_min_ngn from intent
"""

import logging
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from db import save_price_snapshots
from scrapers import build_scrapers
from scrapers.base import Product
from normalizer.core import Normalizer
from normalizer.dedup import ProductGroup
from pipeline.intent import IntentParser, ParsedIntent
from db.cache import QueryCache
import config

logger = logging.getLogger(__name__)

# Sentinel returned when bot should ask a clarification question
# instead of searching. Handlers check for this.
NEEDS_CLARIFICATION = "__needs_clarification__"


class ShoppingPipeline:

    def __init__(self, scraperapi_key=None, use_playwright=True):
        self.scrapers = build_scrapers(
            scraperapi_key=scraperapi_key,
            use_playwright=use_playwright,
        )
        self.normalizer   = Normalizer()
        self.intent_parser = IntentParser()
        self.cache        = QueryCache()

        logger.info(f"[Pipeline] Initialized with scrapers: {list(self.scrapers.keys())}")

    # ── Public API ────────────────────────────────────────────

    async def search_async(
        self,
        query: str,
        top_n: int = config.TOP_N_RESULTS,
        sources: Optional[list] = None,
    ) -> tuple[list[ProductGroup], ParsedIntent]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.search(query, top_n=top_n, sources=sources),
        )

    def search(
        self,
        query: str,
        top_n: int = config.TOP_N_RESULTS,
        sources: Optional[list] = None,
    ) -> tuple[list[ProductGroup], ParsedIntent]:
        t0 = time.monotonic()

        # 1. Parse intent
        intent = self.intent_parser.parse(query)
        logger.info(f"[Pipeline] '{query}' → {intent}")

        # 2. Cost model check — if LLM says ask, don't search yet
        #    Handlers detect empty groups + intent.needs_clarification
        if intent.needs_clarification and not intent.should_search():
            logger.info(
                f"[Pipeline] Ambiguity={intent.ambiguity:.2f} > threshold — "
                f"returning clarification request: '{intent.clarification_question}'"
            )
            return [], intent

        # 3. Check cache — keyed on query + budget + brand + stores
        cache_key = intent.cache_key()
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info(f"[Pipeline] Cache hit ({cache_key})")
            return cached, intent

        # 4. Select scrapers
        active_scrapers = {
            name: s
            for name, s in self.scrapers.items()
            if (sources is None or name in sources)
            and (not intent.store_filter or name in intent.store_filter)
        }
        if not active_scrapers:
            logger.warning("[Pipeline] No active scrapers match request")
            return [], intent

        # 5. Primary search with canonical term
        all_products = self._scrape_parallel(
            intent.canonical_search_term,
            active_scrapers,
        )

        # 6. Candidate fallback — if canonical returned very few results,
        #    search the alternative terms and merge
        if len(all_products) < 3 and len(intent.candidate_terms) > 1:
            logger.info(
                f"[Pipeline] Only {len(all_products)} results from canonical — "
                f"trying {len(intent.candidate_terms)-1} candidate terms"
            )
            seen_urls = {p.url for p in all_products}
            for candidate in intent.candidate_terms[1:]:
                if candidate == intent.canonical_search_term:
                    continue
                candidate_products = self._scrape_parallel(candidate, active_scrapers)
                new_products = [p for p in candidate_products if p.url not in seen_urls]
                if new_products:
                    logger.info(f"[Pipeline] Candidate '{candidate}' added {len(new_products)} products")
                    all_products.extend(new_products)
                    seen_urls.update(p.url for p in new_products)
                if len(all_products) >= 5:
                    break  # enough — don't over-query

        if not all_products:
            logger.warning("[Pipeline] Zero results from all scrapers")
            return [], intent

        # 7. Normalize + rank
        groups = self.normalizer.normalize(
            all_products,
            query=intent.canonical_search_term,
            budget_ngn=intent.budget_ngn,
            budget_min_ngn=intent.budget_min_ngn,
            top_n=top_n,
        )

        elapsed = time.monotonic() - t0
        logger.info(
            f"[Pipeline] Done in {elapsed:.1f}s — "
            f"{len(all_products)} products → {len(groups)} groups"
        )

        # 8. Cache result with rich key
        self.cache.set(cache_key, groups)
        save_price_snapshots(groups)

        return groups, intent

    # ── Parallel scraping ─────────────────────────────────────

    def _scrape_parallel(self, query: str, scrapers: dict) -> list[Product]:
        all_products: list[Product] = []
        max_workers = min(len(scrapers), config.MAX_CONCURRENT_TOTAL)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._scrape_one, name, scraper, query): name
                for name, scraper in scrapers.items()
            }
            try:
                for future in as_completed(futures, timeout=150):
                    store = futures[future]
                    try:
                        products = future.result()
                        count = len(products)
                        logger.info(f"[Pipeline] {store}: {count} results")
                        if count == 0 and config.ALERT_ON_ZERO_RESULTS:
                            logger.warning(f"[Pipeline] ⚠️ {store} returned 0 results")
                        all_products.extend(products)
                    except Exception as e:
                        logger.error(f"[Pipeline] {store} failed: {e}")
            except TimeoutError:
                remaining = [futures[f] for f in futures if not f.done()]
                logger.warning(f"[Pipeline] Timed out waiting for: {remaining}")

        return all_products

    def _scrape_one(self, name: str, scraper, query: str) -> list[Product]:
        try:
            return scraper.search(query, max_results=config.MAX_RESULTS_PER_STORE)
        except Exception as e:
            logger.error(f"[Pipeline] Scraper '{name}' raised: {e}")
            return []

    def add_scraper(self, name: str, scraper) -> None:
        self.scrapers[name] = scraper

    def remove_scraper(self, name: str) -> None:
        self.scrapers.pop(name, None)

    def scraper_health(self) -> dict:
        return {name: "active" for name in self.scrapers}