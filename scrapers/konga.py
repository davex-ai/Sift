"""
Konga Nigeria scraper.

Uses Konga's internal APIs directly — discovered via network capture.
No Playwright or ScraperAPI needed.

Primary:  POST https://api.konga.com/v1/graphql
Fallback: POST https://kss.igbimo.com/search
"""

import re
import logging
import json
import requests as req
from urllib.parse import quote_plus
from typing import Optional

from .base import BaseScraper, Product
from utils.currency import parse_ngn

logger = logging.getLogger(__name__)
KONGA_BASE = "https://www.konga.com"

# ── Confirmed from network capture ────────────────────────────
GRAPHQL_URL = "https://api.konga.com/v1/graphql"
KSS_URL     = "https://kss.igbimo.com/search"
KSS_API_KEY = "kss_pub_BlDPgUB4XUJwJgh7oyliGBFASQLAXR1i4"

GRAPHQL_QUERY = """{{
    searchByStore (
        search_term: [], numericFilters: [], sortBy: "",
        query: "{query}",
        paginate: {{page: 0, limit: {limit}}},
        store_id: 1
    ) {{
        pagination {{limit, page, total}}
        products {{
            name url_key sku brand
            price special_price deal_price final_price original_price
            image_thumbnail image_thumbnail_path
            stock {{in_stock quantity}}
            product_rating {{
                quality {{average number_of_ratings}}
            }}
            seller {{name is_konga is_premium}}
            is_official_store_product
        }}
    }}
}}"""


class KongaScraper(BaseScraper):

    SOURCE_NAME = "konga"
    PROFILE = "konga"
    SELECTORS = {}  # Not used — API-based scraper

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        logger.info(f"[Konga] Searching: {query}")

        # Primary: GraphQL
        products = self._search_graphql(query, max_results)
        if products:
            logger.info(f"[Konga] Found {len(products)} products via GraphQL")
            return products

        # Fallback: KSS Igbimo
        logger.info("[Konga] GraphQL empty → trying KSS API")
        products = self._search_kss(query, max_results)
        if products:
            logger.info(f"[Konga] Found {len(products)} products via KSS")
            return products

        logger.warning("[Konga] Both APIs returned 0 results")
        return []

    # ── GraphQL API ───────────────────────────────────────────

    def _search_graphql(self, query: str, max_results: int) -> list[Product]:
        gql_body = GRAPHQL_QUERY.format(
            query=query.replace('"', '\\"'),
            limit=max_results,
        )
        payload = {"query": gql_body}
        headers = {
            "Content-Type":  "application/json",
            "x-app-source":  "kongavthree",
            "x-app-version": "2.0",
            "Referer":       f"{KONGA_BASE}/",
            "Origin":        KONGA_BASE,
            "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        try:
            resp = req.post(GRAPHQL_URL, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            items = (
                data.get("data", {})
                    .get("searchByStore", {})
                    .get("products", [])
            )
            if not items:
                logger.debug(f"[Konga][GraphQL] Response keys: {list(data.keys())}")
                return []

            return [p for p in (self._from_graphql(item) for item in items) if p]

        except Exception as e:
            logger.error(f"[Konga][GraphQL] {e}")
            return []

    def _from_graphql(self, item: dict) -> Optional[Product]:
        title = (item.get("name") or "").strip()
        if not title:
            return None

        url_key = (item.get("url_key") or "").lstrip("/")
        if not url_key:
            return None
        url = f"{KONGA_BASE}/product/{url_key}" if not url_key.startswith("product/") else f"{KONGA_BASE}/{url_key}"

        # Price: use cheapest available price field
        price_ngn = None
        for key in ("deal_price", "special_price", "final_price", "price", "original_price"):
            val = item.get(key)
            if val:
                try:
                    price_ngn = float(val)
                    break
                except (TypeError, ValueError):
                    continue

        # Image
        img = item.get("image_thumbnail_path") or item.get("image_thumbnail") or ""
        if img and not img.startswith("http"):
            img = f"https://www.konga.com/media/catalog/product{img}"

        # Stock
        stock = item.get("stock") or {}
        in_stock = stock.get("in_stock", True)
        availability = "in_stock" if in_stock else "out_of_stock"

        # Rating
        rating = None
        rating_data = (item.get("product_rating") or {}).get("quality") or {}
        try:
            avg = float(rating_data.get("average") or 0)
            if 0 < avg <= 5:
                rating = round(avg, 1)
        except (TypeError, ValueError):
            pass

        review_count = None
        try:
            review_count = int(rating_data.get("number_of_ratings") or 0) or None
        except (TypeError, ValueError):
            pass

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=f"₦{price_ngn:,.2f}" if price_ngn else "",
            currency="NGN",
            rating=rating,
            review_count=review_count,
            image_url=img or None,
            availability=availability,
            fetched_via="konga_graphql",
        )

    # ── KSS Igbimo fallback API ───────────────────────────────

    def _search_kss(self, query: str, max_results: int) -> list[Product]:
        payload = {
            "name":       "catalog_store_konga_ranking",
            "q":          query,
            "page":       1,
            "hitPerPage": max_results,
        }
        headers = {
            "Content-Type": "application/json",
            "kss-api-key":  KSS_API_KEY,
            "Referer":      f"{KONGA_BASE}/",
            "Origin":       KONGA_BASE,
            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        try:
            resp = req.post(KSS_URL, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            # KSS response: {"status":..., "data": [...], "suggestions": [...]}
            items = data.get("data") or []
            if isinstance(items, dict):
                items = items.get("products") or items.get("hits") or []

            if not items:
                logger.debug(f"[Konga][KSS] Response keys: {list(data.keys())}")
                return []

            return [p for p in (self._from_kss(item) for item in items) if p]

        except Exception as e:
            logger.error(f"[Konga][KSS] {e}")
            return []

    def _from_kss(self, item: dict) -> Optional[Product]:
        title = (item.get("name") or "").strip()
        if not title:
            return None

        url_key = (item.get("url_key") or "").lstrip("/")
        if not url_key:
            return None
        url = f"{KONGA_BASE}/product/{url_key}" if not url_key.startswith("product/") else f"{KONGA_BASE}/{url_key}"

        price_ngn = None
        for key in ("price", "deal_price", "special_price"):
            val = item.get(key)
            if val:
                try:
                    price_ngn = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        img = item.get("image_thumbnail") or item.get("thumbnail") or ""
        if img and not img.startswith("http"):
            img = f"https://www.konga.com/media/catalog/product{img}"

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=f"₦{price_ngn:,.2f}" if price_ngn else "",
            currency="NGN",
            image_url=img or None,
            availability="in_stock",
            fetched_via="konga_kss",
        )

    # ── Required abstract stubs ───────────────────────────────

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        # Not used — this scraper is API-only
        return []