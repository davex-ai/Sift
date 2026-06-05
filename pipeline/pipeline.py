"""
ShoppingPipeline — Perplexity-style research orchestrator.

Flow:
  User query
    → IntentParser (extract budget, category, clean query)
    → Check cache (return instantly if fresh)
    → Parallel scrape all enabled stores
        ├── ScraperAPI (primary)
        └── Playwright fallback
    → Normalizer (dedup + cross-store grouping + scoring)
    → LLM synthesis (Qwen recommendation)
    → Return ranked ProductGroups + LLM text

Like Perplexity: search everywhere simultaneously,
then synthesize a single coherent answer.
"""

import logging
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from scrapers import build_scrapers
from scrapers.base import Product
from normalizer.core import Normalizer
from normalizer.dedup import ProductGroup
from pipeline.intent import IntentParser, SearchIntent
from db.cache import QueryCache
import config

logger = logging.getLogger(__name__)


class ShoppingPipeline:
    """
    Main search pipeline.
    Async-compatible: async search() wraps sync work in thread pool.
    """

    def __init__(
        self,
        scraperapi_key: Optional[str] = None,
        use_playwright: bool = True,
    ):
        self.scrapers = build_scrapers(
            scraperapi_key=scraperapi_key,
            use_playwright=use_playwright,
        )
        self.normalizer = Normalizer()
        self.intent_parser = IntentParser()
        self.cache = QueryCache()

        logger.info(
            f"[Pipeline] Initialized with scrapers: {list(self.scrapers.keys())}"
        )

    # ── Public API ────────────────────────────────────────────

    async def search_async(
        self,
        query: str,
        top_n: int = config.TOP_N_RESULTS,
        sources: Optional[list[str]] = None,
    ) -> tuple[list[ProductGroup], SearchIntent]:
        """
        Async entry point for the Telegram bot.
        Runs blocking scrape work in a thread pool.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.search(query, top_n=top_n, sources=sources),
        )

    def search(
        self,
        query: str,
        top_n: int = config.TOP_N_RESULTS,
        sources: Optional[list[str]] = None,
    ) -> tuple[list[ProductGroup], SearchIntent]:
        """
        Sync search. Returns (ranked groups, parsed intent).
        """
        t0 = time.monotonic()

        # 1. Parse intent
        intent = self.intent_parser.parse(query)
        logger.info(f"[Pipeline] Query: '{query}' → {intent}")

        # 2. Check cache
        cached = self.cache.get(intent.clean_query)
        if cached is not None:
            logger.info("[Pipeline] Cache hit — returning instantly")
            return cached, intent

        # 3. Select scrapers
        active_scrapers = {
            name: s
            for name, s in self.scrapers.items()
            if (sources is None or name in sources)
            and (not intent.store_filter or name in intent.store_filter)
        }
        if not active_scrapers:
            logger.warning("[Pipeline] No active scrapers match request")
            return [], intent

        # 4. Parallel scrape
        all_products = self._scrape_parallel(
            intent.clean_query,
            active_scrapers,
        )

        if not all_products:
            logger.warning("[Pipeline] Zero results from all scrapers")
            return [], intent

        # 5. Normalize + rank
        groups = self.normalizer.normalize(
            all_products,
            query=intent.clean_query,
            budget_ngn=intent.budget_ngn,
            top_n=top_n,
        )

        elapsed = time.monotonic() - t0
        logger.info(
            f"[Pipeline] Done in {elapsed:.1f}s — "
            f"{len(all_products)} products → {len(groups)} groups"
        )

        # 6. Cache result
        self.cache.set(intent.clean_query, groups)

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
                # as_completed processes each result the moment it arrives
                # timeout=150 gives ScraperAPI(55s) + Playwright(60s) room to breathe
                for future in as_completed(futures, timeout=150):
                    store = futures[future]
                    try:
                        products = future.result()
                        count = len(products)
                        logger.info(f"[Pipeline] {store}: {count} results")
                        if count == 0 and config.ALERT_ON_ZERO_RESULTS:
                            logger.warning(
                                f"[Pipeline] ⚠️  {store} returned 0 results — "
                                "selector may be stale"
                            )
                        all_products.extend(products)
                    except Exception as e:
                        logger.error(f"[Pipeline] {store} failed: {e}")

            except TimeoutError:
                remaining = [futures[f] for f in futures if not f.done()]
                logger.warning(f"[Pipeline] Timed out waiting for: {remaining}")

        return all_products

    def _scrape_one(self, name: str, scraper, query: str) -> list[Product]:
        """Single scraper call with per-store error isolation."""
        try:
            return scraper.search(query, max_results=config.MAX_RESULTS_PER_STORE)
        except Exception as e:
            logger.error(f"[Pipeline] Scraper '{name}' raised: {e}")
            return []

    # ── Runtime scraper management ────────────────────────────

    def add_scraper(self, name: str, scraper) -> None:
        """Plug in a new scraper at runtime without restart."""
        self.scrapers[name] = scraper
        logger.info(f"[Pipeline] Registered scraper: {name}")

    def remove_scraper(self, name: str) -> None:
        self.scrapers.pop(name, None)
        logger.info(f"[Pipeline] Removed scraper: {name}")

    def scraper_health(self) -> dict[str, str]:
        """Return last-known status per scraper."""
        return {name: "active" for name in self.scrapers}
