"""
Main pipeline orchestrator.
Runs all scrapers in parallel, normalizes results, returns ranked list.

Usage:
    from shopping_scraper import ShoppingPipeline
    
    pipeline = ShoppingPipeline(scraperapi_key="your_key")
    results = pipeline.search("laptop under 500k")
    for r in results:
        print(r.rank, r.title, r.price_display, r.source)
"""

import os
import concurrent.futures
from typing import Optional

from .scrapers.base import Product
from .scrapers.jumia import JumiaScraper
from .scrapers.amazon import AmazonScraper
from .normalizer.normalizer import Normalizer, extract_budget


class ShoppingPipeline:

    def __init__(
        self,
        scraperapi_key: Optional[str] = None,
        use_playwright: bool = True,
        max_results_per_source: int = 10,
    ):
        self.scraperapi_key = scraperapi_key or os.getenv("SCRAPERAPI_KEY")
        self.use_playwright = use_playwright
        self.max_results_per_source = max_results_per_source
        self.normalizer = Normalizer()

        # Registry — add new scrapers here, nothing else changes
        self._scrapers = {
            "jumia": JumiaScraper(
                scraperapi_key=self.scraperapi_key,
                use_playwright=use_playwright,
            ),
            "amazon": AmazonScraper(
                scraperapi_key=self.scraperapi_key,
                use_playwright=use_playwright,
            ),
            # Future:
            # "konga": KongaScraper(...),
            # "slot": SlotScraper(...),
        }

    def search(
        self,
        query: str,
        sources: Optional[list[str]] = None,  # None = all sources
        top_n: int = 10,
    ):
        """
        Run query across all (or specified) sources in parallel.
        Returns ranked NormalizedResult list.
        """
        active_scrapers = {
            name: scraper
            for name, scraper in self._scrapers.items()
            if sources is None or name in sources
        }

        budget_ngn = extract_budget(query)
        if budget_ngn:
            print(f"[Pipeline] Detected budget: ₦{budget_ngn:,.0f}")

        # Run all scrapers in parallel
        all_products: list[Product] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(active_scrapers)) as executor:
            future_to_source = {
                executor.submit(
                    scraper.search, query, self.max_results_per_source
                ): name
                for name, scraper in active_scrapers.items()
            }

            for future in concurrent.futures.as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    products = future.result()
                    print(f"[Pipeline] {source}: {len(products)} results")
                    all_products.extend(products)
                except Exception as e:
                    print(f"[Pipeline] {source} failed: {e}")

        if not all_products:
            print("[Pipeline] No results from any source")
            return []

        # Normalize + rank
        results = self.normalizer.normalize(
            all_products,
            query=query,
            budget_ngn=budget_ngn,
            top_n=top_n,
        )

        return results

    def add_scraper(self, name: str, scraper):
        """Plug in a new scraper at runtime."""
        self._scrapers[name] = scraper
        print(f"[Pipeline] Added scraper: {name}")
