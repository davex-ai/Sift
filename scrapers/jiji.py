"""
Jiji Nigeria scraper.
URL: https://jiji.ng/search?query=<query>
https://jiji.ng/blender
Jiji is a classifieds marketplace — listings come from individuals
and small businesses. Prices can vary wildly. Includes location,
condition (new/used), and seller info.

Extra fields: location, condition, seller_name.
"""

import re
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional

from .base import BaseScraper, Product
from utils.currency import parse_ngn

logger = logging.getLogger(__name__)
JIJI_BASE = "https://jiji.ng"


class JijiScraper(BaseScraper):

    SOURCE_NAME = "jiji"
    PROFILE = "generic"

    SELECTORS = {
        "product_card": [
            "li.b-list-advert-base",
            "li[class*='b-list-advert']",
            "article[class*='js-advert']",
            "div[class*='advert-list'] li",
        ],
        "title": [
            "a[class*='qa-advert-title']",
            "[class*='title'][class*='advert']",
            "h3[class*='title']",
            "a[class*='advert'] span",
        ],
        "price": [
            "[class*='qa-advert-price']",
            "[class*='price'][class*='advert']",
            "p[class*='price']",
            "span[class*='price']",
        ],
        "image": [
            "img[class*='js-advt-image']",
            "img[class*='advert-image']",
            "div[class*='image'] img",
            "figure img",
        ],
        "link": [
            "a[class*='qa-advert-title']",
            "a[class*='advert-link']",
            "a[href*='/advert/']",
            "a[class*='b-advert-link']",
        ],
        "location": [
            "span[class*='region']",
            "span[class*='location']",
            "[class*='b-advert-list-item__region']",
        ],
        "condition": [
            "[class*='condition']",
            "span[class*='tag']",
        ],
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{JIJI_BASE}/q?query={quote_plus(query)}"
        logger.info(f"[Jiji] Searching: {query}")

        html = self._fetch_with_retry(url)
        if not html:
            logger.error(f"[Jiji] No HTML for query: {query}")
            return []

        products = self._parse_html(html, max_results)
        logger.info(f"[Jiji] Found {len(products)} products")
        return products

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")

        cards = self.try_selectors_all(soup, self.SELECTORS["product_card"])
        if not cards:
            logger.warning("[Jiji] No product cards found — selectors may be stale")
            return []

        products = []
        for card in cards[:max_results]:
            try:
                p = self._parse_card(card)
                if p:
                    products.append(p)
            except Exception as e:
                logger.debug(f"[Jiji] Card parse error: {e}")
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
            link_el = title_el if title_el.name == "a" else None
        url = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            url = href if href.startswith("http") else urljoin(JIJI_BASE, href)
        if not url:
            return None

        # Price — Jiji shows "Negotiable" sometimes
        price_el = self.try_selectors(card, self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price_ngn = parse_ngn(price_raw)

        # Jiji shows "Negotiable" or "Contact seller" for some listings
        is_negotiable = bool(re.search(r"negotia|contact|call", price_raw, re.I))

        # Image
        img_el = self.try_selectors(card, self.SELECTORS["image"])
        image_url = None
        if img_el:
            image_url = (
                img_el.get("data-src")
                or img_el.get("data-lazy-src")
                or img_el.get("src")
            )
            if image_url and ("data:" in image_url or "placeholder" in image_url.lower()):
                image_url = None

        # Location
        loc_el = self.try_selectors(card, self.SELECTORS["location"])
        location = loc_el.get_text(strip=True) if loc_el else None

        # Condition (new / used / refurbished)
        cond_el = self.try_selectors(card, self.SELECTORS["condition"])
        condition = "unknown"
        if cond_el:
            cond_text = cond_el.get_text(strip=True).lower()
            if "new" in cond_text:
                condition = "new"
            elif "used" in cond_text or "fairly" in cond_text:
                condition = "used"
            elif "refurb" in cond_text:
                condition = "refurbished"

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            image_url=image_url,
            availability="in_stock" if price_ngn else "unknown",
            location=location,
            condition=condition,
            fetched_via="scraperapi",
            extra={
                "negotiable": is_negotiable,
                "location": location,
            },
        )
