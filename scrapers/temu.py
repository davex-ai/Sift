"""
Temu scraper.
URL: https://www.temu.com/search_result.html?search_key=<query>

Temu has aggressive bot detection. ScraperAPI is strongly preferred.
Playwright fallback applies extra stealth patches specific to Temu.

Prices on Temu.com are in USD — we convert to NGN.
If ScraperAPI is unavailable, this scraper will likely return 0 results
and log a warning.
"""

import re
import json
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlencode
from typing import Optional

from .base import BaseScraper, Product
from utils.currency import parse_ngn, to_ngn, format_ngn
import config

logger = logging.getLogger(__name__)
TEMU_BASE = "https://www.temu.com"


class TemuScraper(BaseScraper):

    SOURCE_NAME = "temu"
    PROFILE = "generic"

    # Temu uses dynamic class names — we rely on data attributes + aria
    SELECTORS = {
        "product_card": [
            "[class*='goods-column'] li",
            "[data-goods-id]",
            "[class*='search-goods']",
            "div[class*='product-item']",
        ],
        "title": [
            "[class*='goods-title']",
            "[class*='product-title']",
            "h3[class*='title']",
            "[aria-label*='product']",
        ],
        "price": [
            "[class*='price-current']",
            "[class*='goods-price']",
            "[class*='sale-price']",
            "span[class*='price']:not([class*='original'])",
        ],
        "image": [
            "img[src*='img.kwcdn.com']",
            "img[src*='goods']",
            "img[class*='goods-image']",
            "picture source",
        ],
        "link": [
            "a[href*='/goods']",
            "a[href*='/product']",
        ],
        "rating": [
            "[aria-label*='stars']",
            "[class*='star-count']",
            "[class*='rating']",
        ],
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        if not config.SCRAPERAPI_KEY:
            logger.warning(
                "[Temu] No ScraperAPI key — Temu has aggressive bot detection. "
                "Results may be empty. Add SCRAPERAPI_KEY to .env to enable Temu."
            )

        url = (
            f"{TEMU_BASE}/search_result.html"
            f"?search_key={quote_plus(query)}"
            f"&search_method=user"
        )
        logger.info(f"[Temu] Searching: {query}")

        # Temu: try ScraperAPI with JS render first, then Playwright
        html = self._fetch_with_retry(url)

        # Also try Temu's NG-specific version
        if not html:
            url_ng = f"{TEMU_BASE}/ng-en/search_result.html?search_key={quote_plus(query)}"
            html = self._fetch_with_retry(url_ng)

        if not html:
            logger.error("[Temu] All fetch strategies failed")
            return []

        # Try JSON embedded state first (faster than CSS parsing)
        products = self._extract_from_json(html, max_results)
        if products:
            logger.info(f"[Temu] Found {len(products)} products (JSON state)")
            return products

        # Fallback to HTML parsing
        products = self._parse_html(html, max_results)
        logger.info(f"[Temu] Found {len(products)} products (HTML parse)")
        return products

    def _extract_from_json(self, html: str, max_results: int) -> list[Product]:
        import re, json

        # Pattern 1: Next.js standard data island — most reliable
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if m:
            try:
                data = json.loads(m.group(1))
                products = self._parse_goods_json(data, max_results)
                if products:
                    logger.info(f"[Temu] Extracted via __NEXT_DATA__")
                    return products
            except Exception as e:
                logger.debug(f"[Temu] __NEXT_DATA__ parse fail: {e}")

        # Pattern 2: window.__NEXT_DATA__ assigned in script tag
        m = re.search(r'window\.__NEXT_DATA__\s*=\s*({.*?})\s*;?\s*</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                products = self._parse_goods_json(data, max_results)
                if products:
                    return products
            except Exception:
                pass

        # Pattern 3: Inline JSON blobs containing goodsList
        for pattern in [
            r'"goodsList"\s*:\s*(\[.*?\])\s*[,}]',
            r'"goods_list"\s*:\s*(\[.*?\])\s*[,}]',
            r'"searchGoodsList"\s*:\s*(\[.*?\])\s*[,}]',
            r'"result"\s*:\s*\{[^}]*"goods"\s*:\s*(\[.*?\])',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    items = json.loads(m.group(1))
                    products = self._parse_goods_json(items, max_results)
                    if products:
                        return products
                except Exception:
                    continue

        return []

    def _parse_goods_json(self, data: dict | list, max_results: int) -> list[Product]:
        goods_list = []

        def walk(obj, depth=0):
            if depth > 12:  # don't recurse forever
                return
            if isinstance(obj, list):
                for item in obj:
                    walk(item, depth + 1)
            elif isinstance(obj, dict):
                # Broader match — any dict with a name-like and price-like field
                has_name = any(k in obj for k in ("goods_name", "title", "name", "product_name", "display_name"))
                has_price = any(k in obj for k in ("price", "salePrice", "sale_price", "original_price", "goods_price"))
                has_id = any(k in obj for k in ("goods_id", "id", "product_id", "item_id"))
                if has_name and (has_price or has_id):
                    goods_list.append(obj)
                else:
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            walk(v, depth + 1)

        walk(data)

        products = []
        for item in goods_list[:max_results]:
            try:
                p = self._product_from_json(item)
                if p:
                    products.append(p)
            except Exception:
                continue
        return products

    def _product_from_json(self, item: dict) -> Optional[Product]:
        title = (
            item.get("goods_name")
            or item.get("title")
            or item.get("name", "")
        ).strip()
        if not title:
            return None

        # Price — Temu shows cents (e.g. 999 = $9.99)
        raw_price = item.get("price") or item.get("salePrice") or item.get("sale_price", 0)
        price_usd = None
        try:
            if isinstance(raw_price, (int, float)):
                price_usd = raw_price / 100 if raw_price > 500 else raw_price
            elif isinstance(raw_price, str):
                price_usd = float(re.sub(r"[^\d.]", "", raw_price))
        except Exception:
            pass

        price_ngn = to_ngn(price_usd, "USD") if price_usd else None

        goods_id = item.get("goods_id") or item.get("id", "")
        url = f"{TEMU_BASE}/goods.html?_bg_fs=1&goods_id={goods_id}" if goods_id else TEMU_BASE

        image_url = item.get("thumb_url") or item.get("image_url") or item.get("img_url")

        rating_raw = item.get("satisfaction") or item.get("star") or item.get("rate")
        rating = None
        if rating_raw:
            try:
                r = float(rating_raw)
                # Temu satisfaction is often 0-100 — normalize
                rating = round(r / 20, 1) if r > 5 else r
            except Exception:
                pass

        review_count_raw = item.get("comment_num") or item.get("review_count") or item.get("sold_num")
        review_count = None
        try:
            review_count = int(review_count_raw) if review_count_raw else None
        except Exception:
            pass

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=f"${price_usd:.2f}" if price_usd else "",
            currency="USD",
            rating=rating,
            review_count=review_count,
            image_url=image_url,
            availability="in_stock" if price_ngn else "unknown",
            fetched_via="json_state",
            extra={
                "price_usd": price_usd,
                "goods_id": str(goods_id),
                "source_currency": "USD",
            },
        )

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        """CSS parsing fallback when JSON state isn't available."""
        soup = BeautifulSoup(html, "html.parser")

        cards = self.try_selectors_all(soup, self.SELECTORS["product_card"])
        if not cards:
            logger.warning("[Temu] No product cards found in HTML")
            return []

        products = []
        for card in cards[:max_results]:
            try:
                p = self._parse_card(card)
                if p:
                    products.append(p)
            except Exception as e:
                logger.debug(f"[Temu] Card parse error: {e}")
                continue

        return products

    def _parse_card(self, card) -> Optional[Product]:
        title_el = self.try_selectors(card, self.SELECTORS["title"])
        if not title_el:
            return None
        title = title_el.get_text(strip=True) or title_el.get("aria-label", "")
        if not title:
            return None

        link_el = self.try_selectors(card, self.SELECTORS["link"])
        url = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            url = href if href.startswith("http") else f"{TEMU_BASE}{href}"
        if not url:
            return None

        price_el = self.try_selectors(card, self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price_usd = parse_ngn(price_raw)   # parse_ngn strips symbols
        price_ngn = to_ngn(price_usd, "USD") if price_usd else None

        img_el = self.try_selectors(card, self.SELECTORS["image"])
        image_url = None
        if img_el:
            image_url = img_el.get("src") or img_el.get("srcset", "").split(",")[0].split()[0]

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="USD",
            image_url=image_url,
            availability="in_stock" if price_ngn else "unknown",
            fetched_via="html_parse",
            extra={"price_usd": price_usd},
        )
