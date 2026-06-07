"""
Jumia Nigeria scraper.
URL pattern: https://www.jumia.com.ng/catalog/?q=<query>

Jumia is the most scraper-friendly Nigerian store.
HTML is server-rendered, structure is stable.
Fallback: ScraperAPI → Playwright → plain requests.
"""

import re
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional

from .base import BaseScraper, Product
from utils.currency import parse_ngn

logger = logging.getLogger(__name__)
JUMIA_BASE = "https://www.jumia.com.ng"


class JumiaScraper(BaseScraper):

    SOURCE_NAME = "jumia"
    PROFILE = "jumia"
    BLOCK_IMAGES = False
    PLAYWRIGHT_WAIT_UNTIL = "networkidle"

    SELECTORS = {
        # Primary selectors
        "product_card": "article.prd",
        "title":        "h3.name",
        "price":        "div.prc",
        "old_price":    "div.old",
        "rating":       "div.stars._s",
        "review_count": "div.rev",
        "image":        "img.img",
        "link":         "a.core",
        "brand":        "div.brand",
        # Fallbacks
        "product_card_alt": "div[data-gtm-product-id]",
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{JUMIA_BASE}/catalog/?q={quote_plus(query)}&page=1"
        logger.info(f"[Jumia] Searching: {query}")

        html = self._fetch_with_retry(url)
        if not html:
            logger.error(f"[Jumia] No HTML for query: {query}")
            return []

        products = self._parse_html(html, max_results)
        logger.info(f"[Jumia] Found {len(products)} products")
        return products

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")

        # Try primary selector, then fallback
        cards = soup.select(self.SELECTORS["product_card"])
        if not cards:
            cards = soup.select(self.SELECTORS["product_card_alt"])
        if not cards:
            logger.warning("[Jumia] No product cards found — selector may be stale")
            return []

        products = []
        for card in cards[:max_results]:
            try:
                p = self._parse_card(card)
                if p:
                    products.append(p)
            except Exception as e:
                logger.debug(f"[Jumia] Card parse error: {e}")
                continue

        return products

    def _parse_card(self, card) -> Optional[Product]:
        # Title
        title_el = card.select_one(self.SELECTORS["title"])
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        if not title or title == "Unknown":
            return None

        # Link
        link_el = card.select_one(self.SELECTORS["link"])
        url = urljoin(JUMIA_BASE, link_el["href"]) if link_el and link_el.get("href") else JUMIA_BASE

        # Price (current)
        price_el = card.select_one(self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price_ngn = parse_ngn(price_raw)

        # Old price (for discount display)
        old_price_el = card.select_one(self.SELECTORS["old_price"])
        old_price_raw = old_price_el.get_text(strip=True) if old_price_el else ""

        # Rating
        rating_el = card.select_one(self.SELECTORS["rating"])
        rating = _parse_rating(rating_el.get_text(strip=True)) if rating_el else None

        # Reviews
        rev_el = card.select_one(self.SELECTORS["review_count"])
        review_count = _parse_int(rev_el.get_text(strip=True)) if rev_el else None

        # Image
        img_el = card.select_one(self.SELECTORS["image"])
        image_url = None
        if img_el:
            image_url = img_el.get("data-src") or img_el.get("src")

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            rating=rating,
            review_count=review_count,
            image_url=image_url,
            availability="in_stock" if price_ngn else "unknown",
            fetched_via=getattr(self, "SOURCE_NAME", "jumia"),
            extra={"old_price": old_price_raw},
        )


# ── Helpers ───────────────────────────────────────────────────

def _parse_rating(s: str) -> Optional[float]:
    m = re.search(r"(\d+\.?\d*)", s)
    return float(m.group(1)) if m else None

def _parse_int(s: str) -> Optional[int]:
    cleaned = s.replace(",", "").strip("()")
    m = re.search(r"(\d+)", cleaned)
    return int(m.group(1)) if m else None
