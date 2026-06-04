"""
Amazon scraper.
Amazon is significantly harder than Jumia:
  - Aggressive bot detection (Captcha, 503s, fingerprinting)
  - JS-heavy rendering
  - Frequent HTML structure changes

Strategy:
  1. ScraperAPI with `autoparse=true` — they handle Amazon specifically
  2. Fallback: Playwright stealth with extra evasion
  
ScraperAPI's autoparse for Amazon returns clean JSON — way better than
parsing raw HTML. Use it while you have credits.
"""

import os
import re
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional

from .base import BaseScraper, Product
from ..utils.currency import to_ngn
from ..utils.headers import random_headers


AMAZON_BASE = "https://www.amazon.com"


class AmazonScraper(BaseScraper):

    SOURCE_NAME = "amazon"

    # These break semi-frequently. If parsing fails, check these first.
    SELECTORS = {
        "product_card":  "div[data-component-type='s-search-result']",
        "title":         "h2 span.a-text-normal",
        "price_whole":   "span.a-price-whole",
        "price_frac":    "span.a-price-fraction",
        "rating":        "span.a-icon-alt",
        "review_count":  "span.a-size-base[aria-label]",
        "image":         "img.s-image",
        "link":          "a.a-link-normal.s-no-outline",
        "delivery":      "span[data-csa-c-type='element'][aria-label*='delivery']",
        "sponsored":     "span.s-label-popover-default",  # Skip sponsored listings
    }

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{AMAZON_BASE}/s?k={quote_plus(query)}"

        # Try ScraperAPI autoparse first (returns JSON, no HTML parsing needed)
        if self.scraperapi_key:
            products = self._fetch_via_autoparse(query, max_results)
            if products:
                return products
            print("[Amazon] Autoparse failed, trying raw HTML via ScraperAPI...")
            html = self._fetch_via_scraperapi(url)
        else:
            html = None

        if not html and self.use_playwright:
            print("[Amazon] Falling back to Playwright...")
            html = self._fetch_via_playwright(url)

        if not html:
            print(f"[Amazon] All fetch methods failed for: {query}")
            return []

        return self._parse_html(html, max_results)

    # -----------------------------------------------------------
    # Fetch strategies
    # -----------------------------------------------------------

    def _fetch_via_autoparse(self, query: str, max_results: int) -> list[Product]:
        """
        ScraperAPI's Amazon autoparse — returns structured JSON.
        Costs 10 credits per request (vs 1 for regular).
        Worth it: no fragile selectors, handles anti-bot automatically.
        """
        try:
            resp = requests.get(
                "https://api.scraperapi.com/structured/amazon/search",
                params={
                    "api_key": self.scraperapi_key,
                    "query": query,
                    "country": "us",  # Amazon.com
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_autoparse_json(data, max_results)
        except Exception as e:
            print(f"[Amazon][Autoparse] Error: {e}")
            return []

    def _fetch_via_scraperapi(self, url: str) -> Optional[str]:
        try:
            proxy_url = self._scraperapi_url(url, render_js=True)
            resp = requests.get(proxy_url, timeout=45)
            resp.raise_for_status()
            # Check if Amazon served us a CAPTCHA page
            if "Type the characters" in resp.text or "api-services-support" in resp.text:
                print("[Amazon][ScraperAPI] Got CAPTCHA page")
                return None
            return resp.text
        except Exception as e:
            print(f"[Amazon][ScraperAPI] Error: {e}")
            return None

    def _fetch_via_playwright(self, url: str) -> Optional[str]:
        """
        Extra evasion needed for Amazon vs Jumia.
        Simulates human-like behavior: scroll, random mouse moves.
        """
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import stealth_sync
            import random

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ]
                )
                context = browser.new_context(
                    user_agent=random_headers()["User-Agent"],
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                    # Spoof WebGL to avoid canvas fingerprinting
                    extra_http_headers=random_headers(),
                )
                page = context.new_page()
                stealth_sync(page)

                # Human-like: don't load instantly
                self._random_delay(2.0, 4.0)
                page.goto(url, wait_until="networkidle", timeout=45000)

                # Simulate scroll (triggers lazy-loaded content)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                self._random_delay(1.0, 2.0)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                self._random_delay(0.5, 1.5)

                # Check for CAPTCHA
                if page.query_selector("form[action='/errors/validateCaptcha']"):
                    print("[Amazon][Playwright] Hit CAPTCHA")
                    browser.close()
                    return None

                html = page.content()
                browser.close()
                return html

        except Exception as e:
            print(f"[Amazon][Playwright] Error: {e}")
            return None

    # -----------------------------------------------------------
    # Parse layers
    # -----------------------------------------------------------

    def _parse_autoparse_json(self, data: dict, max_results: int) -> list[Product]:
        """Parse ScraperAPI's structured Amazon JSON response."""
        results = data.get("results", [])[:max_results]
        products = []

        for item in results:
            try:
                price_str = item.get("price", "")
                price_usd = _parse_usd(price_str)
                price_ngn = to_ngn(price_usd, "USD") if price_usd else None

                products.append(Product(
                    title=item.get("name", "Unknown"),
                    price_ngn=price_ngn,
                    price_raw=price_str,
                    currency="USD",
                    url=item.get("url") or f"{AMAZON_BASE}/dp/{item.get('asin', '')}",
                    image_url=item.get("image"),
                    rating=_safe_float(item.get("stars")),
                    review_count=_parse_int(str(item.get("total_reviews", ""))),
                    availability="in_stock" if price_usd else "unknown",
                    source=self.SOURCE_NAME,
                    delivery_info=item.get("delivery"),
                    extra={"asin": item.get("asin"), "is_prime": item.get("is_prime")},
                ))
            except Exception as e:
                print(f"[Amazon][JSON Parse] Skipping item: {e}")
                continue

        return products

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        """Fallback HTML parser — more fragile, update selectors if it breaks."""
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(self.SELECTORS["product_card"])[:max_results]

        products = []
        for card in cards:
            try:
                # Skip sponsored results
                if card.select_one(self.SELECTORS["sponsored"]):
                    continue
                product = self._parse_card(card)
                if product:
                    products.append(product)
            except Exception as e:
                print(f"[Amazon][HTML Parse] Skipping card: {e}")
                continue

        return products

    def _parse_card(self, card) -> Optional[Product]:
        title_el = card.select_one(self.SELECTORS["title"])
        if not title_el:
            return None
        title = title_el.get_text(strip=True)

        link_el = card.select_one(self.SELECTORS["link"])
        url = urljoin(AMAZON_BASE, link_el["href"]) if link_el else AMAZON_BASE

        whole = card.select_one(self.SELECTORS["price_whole"])
        frac = card.select_one(self.SELECTORS["price_frac"])
        if whole:
            price_str = whole.get_text(strip=True).replace(",", "")
            if frac:
                price_str += f".{frac.get_text(strip=True)}"
            price_usd = _safe_float(price_str)
            price_raw = f"${price_str}"
        else:
            price_usd = None
            price_raw = "N/A"

        price_ngn = to_ngn(price_usd, "USD") if price_usd else None

        rating_el = card.select_one(self.SELECTORS["rating"])
        rating = _parse_rating(rating_el.get_text(strip=True)) if rating_el else None

        rev_el = card.select_one(self.SELECTORS["review_count"])
        review_count = _parse_int(rev_el.get("aria-label", "")) if rev_el else None

        img_el = card.select_one(self.SELECTORS["image"])
        image_url = img_el.get("src") if img_el else None

        delivery_el = card.select_one(self.SELECTORS["delivery"])
        delivery = delivery_el.get("aria-label") if delivery_el else None

        return Product(
            title=title,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="USD",
            url=url,
            image_url=image_url,
            rating=rating,
            review_count=review_count,
            availability="in_stock" if price_usd else "unknown",
            source=self.SOURCE_NAME,
            delivery_info=delivery,
        )


# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------

def _parse_usd(price_str: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", price_str)
    return _safe_float(cleaned)

def _safe_float(s) -> Optional[float]:
    try:
        return float(str(s).replace(",", "")) if s else None
    except (ValueError, TypeError):
        return None

def _parse_rating(s: str) -> Optional[float]:
    match = re.search(r"(\d+\.?\d*)\s+out\s+of", s)
    return float(match.group(1)) if match else None

def _parse_int(s: str) -> Optional[int]:
    match = re.search(r"[\d,]+", s)
    return int(match.group().replace(",", "")) if match else None
