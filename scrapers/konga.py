"""
Konga Nigeria scraper.
URL pattern: https://www.konga.com/search?search=<query>

Konga uses React SSR. The product grid is mostly server-rendered.
Multiple selector fallbacks are used since class names can change
between deployments.

Fallback chain: ScraperAPI → Playwright → plain requests.
"""

import re
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional

from .base import BaseScraper, Product
from utils.currency import parse_ngn

logger = logging.getLogger(__name__)
KONGA_BASE = "https://www.konga.com"


class KongaScraper(BaseScraper):

    SOURCE_NAME = "konga"
    PROFILE = "konga"

    # Konga has changed their class names over time.
    # We try multiple patterns — first hit wins.
    SELECTORS = {
        "product_card": [
            "div[class*='product-card']",
            "div[class*='ProductCard']",
            "div[class*='product-list'] > div",
            "section[class*='product'] > div",
            "[data-testid='product-card']",
            "div.col-xs-6",
            "div.col-sm-4",
        ],
        "title": [
            "[class*='product-title']",
            "[class*='ProductTitle']",
            "h3[class*='title']",
            "p[class*='title']",
            "a[class*='title']",
        ],
        "price": [
            "[class*='price']:not([class*='old']):not([class*='strike'])",
            "span[class*='Price']",
            "[class*='product-price']",
            "span.currency",
        ],
        "old_price": [
            "[class*='old-price']",
            "[class*='strike']",
            "[class*='OldPrice']",
        ],
        "rating": [
            "[class*='rating']",
            "[class*='star']",
            "[aria-label*='rating']",
        ],
        "image": [
            "img[class*='product']",
            "img[class*='thumbnail']",
            "div[class*='image'] img",
            "figure img",
        ],
        "link": [
            "a[href*='/product/']",
            "a[href*='/catalog/']",
            "a[class*='product']",
        ],
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{KONGA_BASE}/search?search={quote_plus(query)}&page=1"
        logger.info(f"[Konga] Searching: {query}")

        html = self._fetch_with_retry(url)
        if not html:
            logger.error(f"[Konga] No HTML for query: {query}")
            return []

        products = self._parse_html(html, max_results)
        logger.info(f"[Konga] Found {len(products)} products")
        return products

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")

        # Try each card selector
        cards = self.try_selectors_all(soup, self.SELECTORS["product_card"])
        if not cards:
            logger.warning("[Konga] No product cards — selectors may be stale")
            return []

        products = []
        for card in cards[:max_results]:
            try:
                p = self._parse_card(card)
                if p:
                    products.append(p)
            except Exception as e:
                logger.debug(f"[Konga] Card parse error: {e}")
                continue

        return products

    def _parse_card(self, card) -> Optional[Product]:
        # Title
        title_el = self.try_selectors(card, self.SELECTORS["title"])
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title:
            return None

        # Link
        link_el = self.try_selectors(card, self.SELECTORS["link"])
        if not link_el:
            # Try parent anchor
            link_el = card.find_parent("a") or card.find("a")
        url = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            url = href if href.startswith("http") else urljoin(KONGA_BASE, href)
        if not url:
            return None

        # Price
        price_el = self.try_selectors(card, self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price_ngn = parse_ngn(price_raw)

        # Old price
        old_el = self.try_selectors(card, self.SELECTORS["old_price"])
        old_price = old_el.get_text(strip=True) if old_el else ""

        # Rating
        rating_el = self.try_selectors(card, self.SELECTORS["rating"])
        rating = None
        if rating_el:
            rating_text = rating_el.get_text(strip=True) or rating_el.get("aria-label", "")
            rating = _parse_rating(rating_text)

        # Image
        img_el = self.try_selectors(card, self.SELECTORS["image"])
        image_url = None
        if img_el:
            image_url = (
                img_el.get("data-src")
                or img_el.get("data-lazy-src")
                or img_el.get("src")
            )
            # Filter out data:image placeholder
            if image_url and image_url.startswith("data:"):
                image_url = None

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            rating=rating,
            image_url=image_url,
            availability="in_stock" if price_ngn else "unknown",
            fetched_via="scraperapi",
            extra={"old_price": old_price},
        )


# ── Helpers ───────────────────────────────────────────────────

def _parse_rating(s: str) -> Optional[float]:
    m = re.search(r"(\d+\.?\d*)", s)
    v = float(m.group(1)) if m else None
    return v if v and v <= 5.0 else None
