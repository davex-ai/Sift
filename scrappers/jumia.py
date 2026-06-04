"""
Jumia Nigeria scraper.
Strategy:
  1. Try ScraperAPI (uses credits, reliable)
  2. Fallback: raw Playwright + stealth (free, fragile)

Jumia is more scraper-friendly than Amazon — less aggressive bot detection.
Their HTML structure is relatively stable.
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional

from .base import BaseScraper, Product
from ..utils.currency import to_ngn
from ..utils.headers import random_headers


JUMIA_BASE = "https://www.jumia.com.ng"


class JumiaScraper(BaseScraper):

    SOURCE_NAME = "jumia"

    # -----------------------------------------------------------
    # CSS selectors — update these if Jumia restructures their UI
    # Tip: run `python -m shopping_scraper.utils.selector_check jumia`
    # to verify selectors are still valid
    # -----------------------------------------------------------
    SELECTORS = {
        "product_card": "article.prd",
        "title":        "h3.name",
        "price":        "div.prc",
        "old_price":    "div.old",
        "rating":       "div.stars._s",
        "review_count": "div.rev",
        "image":        "img.img",
        "link":         "a.core",
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{JUMIA_BASE}/catalog/?q={quote_plus(query)}"

        html = self._fetch_html(url)
        if not html:
            print(f"[Jumia] Failed to fetch HTML for query: {query}")
            return []

        return self._parse(html, max_results)

    # -----------------------------------------------------------
    # Fetch layer — ScraperAPI first, Playwright fallback
    # -----------------------------------------------------------

    def _fetch_html(self, url: str) -> Optional[str]:
        if self.scraperapi_key:
            html = self._fetch_via_scraperapi(url)
            if html:
                return html
            print("[Jumia] ScraperAPI failed, falling back to Playwright...")

        if self.use_playwright:
            return self._fetch_via_playwright(url)

        return None

    def _fetch_via_scraperapi(self, url: str) -> Optional[str]:
        try:
            proxy_url = self._scraperapi_url(url, render_js=True)
            resp = requests.get(proxy_url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"[Jumia][ScraperAPI] Error: {e}")
            return None

    def _fetch_via_playwright(self, url: str) -> Optional[str]:
        """
        Stealth Playwright fetch.
        Requires: pip install playwright playwright-stealth
                  playwright install chromium
        """
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import stealth_sync

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                    ]
                )
                context = browser.new_context(
                    user_agent=random_headers()["User-Agent"],
                    viewport={"width": 1366, "height": 768},
                    locale="en-NG",
                    timezone_id="Africa/Lagos",
                )
                page = context.new_page()
                stealth_sync(page)

                self._random_delay(1.5, 3.0)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_selector(self.SELECTORS["product_card"], timeout=10000)

                html = page.content()
                browser.close()
                return html

        except Exception as e:
            print(f"[Jumia][Playwright] Error: {e}")
            return None

    # -----------------------------------------------------------
    # Parse layer
    # -----------------------------------------------------------

    def _parse(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(self.SELECTORS["product_card"])[:max_results]

        products = []
        for card in cards:
            try:
                product = self._parse_card(card)
                if product:
                    products.append(product)
            except Exception as e:
                print(f"[Jumia][Parse] Skipping card: {e}")
                continue

        return products

    def _parse_card(self, card) -> Optional[Product]:
        # Title
        title_el = card.select_one(self.SELECTORS["title"])
        if not title_el:
            return None
        title = title_el.get_text(strip=True)

        # Link
        link_el = card.select_one(self.SELECTORS["link"])
        url = urljoin(JUMIA_BASE, link_el["href"]) if link_el else JUMIA_BASE

        # Price
        price_el = card.select_one(self.SELECTORS["price"])
        price_raw = price_el.get_text(strip=True) if price_el else "N/A"
        price_ngn = _parse_ngn(price_raw)

        # Rating
        rating_el = card.select_one(self.SELECTORS["rating"])
        rating = _parse_rating(rating_el.get_text(strip=True)) if rating_el else None

        # Review count
        rev_el = card.select_one(self.SELECTORS["review_count"])
        review_count = _parse_int(rev_el.get_text(strip=True)) if rev_el else None

        # Image
        img_el = card.select_one(self.SELECTORS["image"])
        image_url = img_el.get("data-src") or img_el.get("src") if img_el else None

        return Product(
            title=title,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            url=url,
            image_url=image_url,
            rating=rating,
            review_count=review_count,
            availability="in_stock" if price_ngn else "unknown",
            source=self.SOURCE_NAME,
            delivery_info=None,  # Jumia shows delivery on product page, not listing
        )


# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

def _parse_ngn(price_str: str) -> Optional[float]:
    """Extract float from '₦ 450,000' or 'NGN 450000' etc."""
    cleaned = re.sub(r"[^\d.]", "", price_str.replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_rating(rating_str: str) -> Optional[float]:
    """Extract float from '4.2 out of 5' or just '4.2'."""
    match = re.search(r"(\d+\.?\d*)", rating_str)
    return float(match.group(1)) if match else None


def _parse_int(s: str) -> Optional[int]:
    match = re.search(r"(\d+)", s.replace(",", ""))
    return int(match.group(1)) if match else None
