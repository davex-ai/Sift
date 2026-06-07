"""
Slot Nigeria scraper.
URL: https://slot.ng/shop?q=<query>

Slot.ng uses a modern Next.js + Tailwind layout.
Specializes in phones, tablets, laptops, accessories.
"""

import time
import random
import re
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from typing import Optional
from utils.headers import session_headers

from .base import BaseScraper, Product
from utils.currency import parse_ngn

logger = logging.getLogger(__name__)
SLOT_BASE = "https://slot.ng"


class SlotScraper(BaseScraper):

    SOURCE_NAME = "slot"
    PROFILE = "generic"

    # We keep SELECTORS empty/minimal to bypass the legacy architecture matching
    # and explicitly override the entry methods for modern safety.
    SELECTORS = {}

    def search(self, query: str, max_results: int = 10) -> list[Product]:
        url = f"{SLOT_BASE}/shop?q={quote_plus(query)}"
        logger.info(f"[Slot] Searching: {query}")

        html = self._fetch_with_retry(url)
        if not html:
            logger.error(f"[Slot] No HTML for query: {query}")
            return []

        with open("slot_debug.html", "w", encoding="utf8") as f:
            f.write(html)

        products = self._parse_html(html, max_results)
        logger.info(f"[Slot] Found {len(products)} products")
        return products

    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")
        products = []

        # Target the explicit shop grid product cards (Scenario B)
        grid_cards = soup.find_all(
            "div", class_=lambda c: c and "group" in c and "flex-col" in c
        )

        for card in grid_cards:
            if len(products) >= max_results:
                break

            try:
                # Contextually filter to ensure it's a real product card
                link_tag = card.find("a", href=re.compile(r"^/products?/"))
                if not link_tag:
                    continue

                p = self._parse_card_context(card, link_tag)
                if p:
                    products.append(p)
            except Exception as e:
                logger.debug(f"[Slot] Card parse error: {e}")
                continue

        # Fallback: If shop grid is empty (e.g. Next.js payload haven't populated yet),
        # scrape the search dropdown payload embedded in the DOM layout (Scenario A)
        if not products:
            dropdown_container = soup.find(
                "div", class_=lambda c: c and "absolute" in c and "max-h-95" in c
            )
            if dropdown_container:
                items = dropdown_container.find_all(
                    "div", class_=lambda c: c and "cursor-pointer" in c
                )
                for item in items:
                    if len(products) >= max_results:
                        break
                    try:
                        p = self._parse_dropdown_item(item)
                        if p:
                            products.append(p)
                    except Exception as e:
                        logger.debug(f"[Slot] Dropdown item parse error: {e}")
                        continue

        return products

    def _parse_card_context(self, card, link_tag) -> Optional[Product]:
        """Parses a product from the main shop grid."""
        img_tag = link_tag.find("img")
        title = img_tag.get("alt") if img_tag else None
        if not title:
            return None

        # Build clean absolute product link
        url = link_tag.get("href", "")
        if url and not url.startswith("http"):
            url = urljoin(SLOT_BASE, url)

        # Get structural text-red container for price
        price_el = card.find("span", class_=lambda c: c and "text-red-600" in c)
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price_ngn = parse_ngn(price_raw)

        image_url = img_tag.get("src") if img_tag else None

        return Product(
            title=title.strip(),
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            rating=None,
            review_count=None,
            image_url=image_url,
            availability="in_stock" if price_ngn else "unknown",
            fetched_via="playwright",
            extra={},
        )

    def _parse_dropdown_item(self, item) -> Optional[Product]:
        """Parses a product fallback from the autocomplete overlay."""
        title_tag = item.find("div", class_=lambda c: c and "truncate" in c)
        price_tag = item.find("div", class_=lambda c: c and "text-red-600" in c)

        if not title_tag or not price_tag:
            return None

        title = title_tag.get_text(strip=True)
        price_raw = price_tag.get_text(strip=True)
        price_ngn = parse_ngn(price_raw)

        # Dropdown links usually target the card component or wrapper parent anchor if available
        link_tag = item.find_parent("a") or item.find("a")
        url = link_tag.get("href", f"/shop?q={quote_plus(title)}") if link_tag else f"/shop?q={quote_plus(title)}"
        if url and not url.startswith("http"):
            url = urljoin(SLOT_BASE, url)

        img_tag = item.find("img")
        image_url = img_tag.get("src") if img_tag else None

        return Product(
            title=title,
            source=self.SOURCE_NAME,
            url=url,
            price_ngn=price_ngn,
            price_raw=price_raw,
            currency="NGN",
            rating=None,
            review_count=None,
            image_url=image_url,
            availability="in_stock",
            fetched_via="playwright",
            extra={"context": "search_dropdown"},
        )

    def _fetch_playwright(self, url: str) -> Optional[str]:
        try:
            from playwright.sync_api import sync_playwright

            fp = self._fingerprint()

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--window-size=1366,768",
                        "--disable-extensions",
                        "--disable-gpu",
                        f"--lang={fp['locale']}",
                    ],
                )
                context = browser.new_context(
                    user_agent=fp["user_agent"],
                    viewport={"width": 1366, "height": 768},
                    locale=fp["locale"],
                    timezone_id=fp["timezone"],
                    extra_http_headers=session_headers(self.SOURCE_NAME),
                )
                context.add_init_script(self._stealth_js(fp))

                page = context.new_page()
                if getattr(self, "BLOCK_IMAGES", True):
                    page.route(
                        "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf,eot}",
                        lambda r: r.abort(),
                    )

                # Inside your _fetch_playwright method, locate this block:
                time.sleep(random.uniform(0.3, 0.8))
                page.goto(url, wait_until="networkidle", timeout=30_000)

                # 🔴 CHANGE THIS PART:
                # Force Playwright to wait for either the main shop grid OR the search text state to finish hydrating
                try:
                    # Look for a common denominator element that proves Next.js hydration completed
                    page.wait_for_selector("div[class*='grid']", timeout=15_000)
                except Exception:
                    # Fallback to waiting for any element tracking an anchor link to slot products
                    try:
                        page.wait_for_selector("a[href*='/product']", timeout=5_000)
                    except Exception:
                        logger.warning(f"[{self.SOURCE_NAME}] Playwright wait timed out; parsing partial HTML.")

                self._human_scroll(page)
                html = page.content()
                browser.close()
                logger.info(f"[{self.SOURCE_NAME}] Playwright OK ({len(html)} bytes)")
                return html

        except ImportError:
            logger.error("[BaseScraper] Playwright not installed.")
            return None
        except Exception as e:
            logger.error(f"[{self.SOURCE_NAME}][Playwright] {e}")
            return None
