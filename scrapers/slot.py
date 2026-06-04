"""
Slot Nigeria scraper.
URL: https://www.slot.ng/shop?q=<query>

Slot.ng uses WooCommerce. HTML structure is stable and clean.
Specializes in phones, tablets, laptops, accessories.
"""

import re
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional

from .base import BaseScraper, Product
from utils.currency import parse_ngn

logger = logging.getLogger(__name__)
SLOT_BASE = "https://www.slot.ng"


class SlotScraper(BaseScraper):

    SOURCE_NAME = "slot"
    PROFILE = "generic"

    # WooCommerce selectors — very stable
    SELECTORS = {
        "product_card": [
            "li.product",
            "li[class*='product-type']",
            "div.product-inner",
        ],
        "title": [
            "h2.woocommerce-loop-product__title",
            "h2.product-title",
            "h3.product-title",
            "[class*='product-title']",
            "a.product-loop-title",
        ],
        "price": [
            "span.woocommerce-Price-amount.amount",
            "bdi",
            "span.price-amount",
            "[class*='price']:not([class*='old'])",
        ],
        "old_price": [
            "del span.woocommerce-Price-amount",
            "del bdi",
            "span.price del",
        ],
        "rating": [
            "div.star-rating",
            "[class*='star-rating']",
            "span[class*='rating']",
        ],
        "review_count": [
            "span.count",
            "span[class*='rating-count']",
        ],
        "image": [
            "img.attachment-woocommerce_thumbnail",
            "img[class*='product-thumbnail']",
            "div.thumbnail-wrapper img",
            "figure img",
        ],
        "link": [
            "a.woocommerce-LoopProduct-link",
            "a[href*='/product/']",
            "a.product-loop-title",
        ],
        "availability": [
            "p.stock.in-stock",
            "p.stock.out-of-stock",
            "[class*='availability']",
        ],
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{SLOT_BASE}/search/shop?q={quote_plus(query)}&post_type=product"
        logger.info(f"[Slot] Searching: {query}")

        html = self._fetch_with_retry(url)
        if not html:
            logger.error(f"[Slot] No HTML for query: {query}")
            return []

        products = self._parse_html(html, max_results)
        logger.info(f"[Slot] Found {len(products)} products")
        return products

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")

        cards = self.try_selectors_all(soup, self.SELECTORS["product_card"])
        if not cards:
            logger.warning("[Slot] No product cards — WooCommerce selectors may be stale")
            return []

        products = []
        for card in cards[:max_results]:
            try:
                p = self._parse_card(card)
                if p:
                    products.append(p)
            except Exception as e:
                logger.debug(f"[Slot] Card parse error: {e}")
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
        url = ""
        if link_el and link_el.get("href"):
            url = link_el["href"]
            if not url.startswith("http"):
                url = urljoin(SLOT_BASE, url)
        if not url:
            return None

        # Price — WooCommerce has nested bdi tag
        price_el = self.try_selectors(card, self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price_ngn = parse_ngn(price_raw)

        # Old price
        old_el = self.try_selectors(card, self.SELECTORS["old_price"])
        old_price = old_el.get_text(strip=True) if old_el else ""

        # Rating (WooCommerce star ratings)
        rating_el = self.try_selectors(card, self.SELECTORS["rating"])
        rating = None
        if rating_el:
            # WooCommerce: aria-label="Rated 4.00 out of 5"
            aria = rating_el.get("aria-label", "")
            if not aria:
                aria = rating_el.get_text(strip=True)
            rating = _parse_rating(aria)

        # Review count
        rev_el = self.try_selectors(card, self.SELECTORS["review_count"])
        review_count = None
        if rev_el:
            m = re.search(r"(\d+)", rev_el.get_text())
            review_count = int(m.group(1)) if m else None

        # Image
        img_el = self.try_selectors(card, self.SELECTORS["image"])
        image_url = None
        if img_el:
            image_url = (
                img_el.get("data-src")
                or img_el.get("data-lazy-src")
                or img_el.get("src")
            )

        # Availability
        avail_el = self.try_selectors(card, self.SELECTORS["availability"])
        availability = "unknown"
        if avail_el:
            text = avail_el.get_text(strip=True).lower()
            if "in stock" in text or "available" in text:
                availability = "in_stock"
            elif "out of stock" in text:
                availability = "out_of_stock"
        elif price_ngn:
            availability = "in_stock"

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
            availability=availability,
            fetched_via="scraperapi",
            extra={"old_price": old_price},
        )


# ── Helpers ───────────────────────────────────────────────────

def _parse_rating(s: str) -> Optional[float]:
    m = re.search(r"(\d+\.?\d*)", s)
    v = float(m.group(1)) if m else None
    return v if v and v <= 5.0 else None
