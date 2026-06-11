"""
Jiji Nigeria Scraper.
Optimized Strategy:
  1. Primary: JSON API requested via ScraperAPI (Raw text mode, no JS engine overhead).
  2. Fallback: Full Dynamic HTML rendering handled exclusively by Playwright Stealth.
"""

import re
import logging
import json
import requests as req
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

    # ── Subclass Properties targeting BaseScraper behavior configurations ──
    BLOCK_IMAGES_AND_FONTS = False  # Keep imagery pipelines active so anti-bot challenges resolve
    PLAYWRIGHT_WAIT_UNTIL = "networkidle"
    SETTLE_TIME_MS = 10000          # The 10-second wait needed specifically for Jiji's anti-bot engine
    SHOULD_HUMAN_SCROLL = True
    HYDRATION_SELECTOR = "div.b-list-advert__gallery, div[data-testid='listing-item'], .b-trending-card"

    SELECTORS = {
        "product_card": [
            "div.b-list-advert-base",
            "div[data-testid='listing-item']",
            "div.b-trending-card",
            "div.b-list-advert__gallery-item",
            "div.masonry-item",
            "div.js-advert-card"
        ],
        "title": [
            "div.b-advert-title-inner",
            "a[href*='/advert/'] div[class*='title']",
            "div[data-testid='listing-title']",
            "h3",
        ],
        "price": [
            "div.b-list-advert-base__price",
            "div[class*='price']",
            "span[class*='price']",
            "p[class*='price']",
        ],
        "image": [
            "div.b-list-advert-base__img img",
            "picture img",
            "img[class*='js-advt-image']",
            "img"
        ],
        "link": [
            "a.b-list-advert-base__heading-link",
            "a[href*='/advert/']",
            "a",
        ],
        "location": [
            "span.b-list-advert-base__region-text",
            "div[class*='location']",
            "span[class*='region']",
        ],
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        # 1. Try JSON API routed through ScraperAPI (Static proxy mode)
        products = self._search_json_api(query, max_results)
        if products:
            logger.info(f"[Jiji] Found {len(products)} products via JSON API")
            return products

        # 2. HTML Fallback via Unifed Playwright inherited setup
        logger.info(f"[Jiji] API route failed or empty. Forcing Playwright Dynamic Scrape...")
        url = f"{JIJI_BASE}/search?query={quote_plus(query)}"

        html = None
        if self.use_playwright:
            html = self._fetch_with_retry(url) # Pull through base tracking engine wrapper safely

        if not html:
            logger.warning(f"[Jiji] Playwright failed to capture page source markup.")
            return []

        products = self._parse_html(html, max_results)
        logger.info(f"[Jiji] Found {len(products)} products (HTML Parser)")
        return products

    # ── JSON API (Proxied via ScraperAPI) ──────────────────────
    def _search_json_api(self, query: str, max_results: int) -> list[Product]:
        target_api_url = (
            f"{JIJI_BASE}/api_web/v1/listing"
            f"?query={quote_plus(query)}"
            f"&webp=true&page=1&per_page={max_results}"
        )

        if not self.scraperapi_key:
            return []

        proxied_url = self._scraperapi_url(target_api_url, render_js=False)

        try:
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            }
            resp = req.get(proxied_url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return []

            data = resp.json()
            adverts = data.get("adverts") or data.get("adverts_list") or (data.get("data") or {}).get("adverts") or []

            if not adverts:
                return []

            products = []
            for advert in adverts[:max_results]:
                try:
                    p = self._advert_from_json(advert)
                    if p:
                        products.append(p)
                except Exception:
                    pass
            return products
        except Exception:
            return []

    def _advert_from_json(self, advert: dict) -> Optional[Product]:
        title = (advert.get("title") or advert.get("name") or "").strip()
        if not title:
            return None

        slug = advert.get("slug") or advert.get("url") or ""
        url = slug if slug.startswith("http") else f"{JIJI_BASE}/{slug.lstrip('/')}"

        price_obj = advert.get("price_obj") or {}
        price_ngn = None
        if price_obj.get("value"):
            try:
                price_ngn = float(price_obj["value"])
            except (TypeError, ValueError):
                pass
        if not price_ngn:
            price_ngn = parse_ngn(str(advert.get("price") or ""))
        price_raw = price_obj.get("view") or str(advert.get("price") or "")

        photos = advert.get("photos") or advert.get("images") or []
        image_url = photos[0] if photos and isinstance(photos[0], str) else photos[0].get("url") if photos and isinstance(photos[0], dict) else None

        region = advert.get("region") or {}
        location = region.get("name") if isinstance(region, dict) else str(region or "")

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            image_url=image_url or None,
            availability="in_stock" if price_ngn else "unknown",
            location=location or None,
            fetched_via="jiji_api",
        )

    # ── HTML Fallback Engine ──────────────────────────────────
    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")
        cards = self.try_selectors_all(soup, self.SELECTORS["product_card"])
        if not cards:
            return []

        products = []
        for card in cards[:max_results]:
            try:
                p = self._parse_card(card)
                if p:
                    products.append(p)
            except Exception:
                pass
        return products

    def _parse_card(self, card) -> Optional[Product]:
        link_el = self.try_selectors(card, self.SELECTORS["link"])
        if not link_el or not link_el.get("href"):
            link_el = card if card.name == "a" else card.find("a")
        if not link_el or not link_el.get("href"):
            return None

        href = link_el["href"]
        url = href if href.startswith("http") else urljoin(JIJI_BASE, href)

        title_el = self.try_selectors(card, self.SELECTORS["title"])
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            return None

        price_el = self.try_selectors(card, self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else ""
        if price_raw.lower() in ("negotiable", "call", "free", "contact"):
            availability = "negotiable"
            price_ngn = None
        price_ngn = parse_ngn(price_raw)

        img_el = self.try_selectors(card, self.SELECTORS["image"])
        image_url = img_el.get("data-src") or img_el.get("src") if img_el else None

        loc_el = self.try_selectors(card, self.SELECTORS["location"])
        location = loc_el.get_text(strip=True) if loc_el else None

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
            fetched_via="html_parse",
        )