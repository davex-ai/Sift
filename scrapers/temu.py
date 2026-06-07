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
    # self._load_temu_cookies(context)

    def _load_temu_cookies(self, context) -> bool:
        """
        Load saved Temu cookies from temu_cookies.json.
        Export from Chrome: EditThisCookie extension → Export.
        """
        import json
        from pathlib import Path

        cookie_file = Path("temu_cookies.json")
        if not cookie_file.exists():
            logger.debug("[Temu] No cookie file found — skipping cookie injection")
            return False

        try:
            raw = json.loads(cookie_file.read_text())

            # Chrome exports in slightly different format than Playwright expects
            cookies = []
            for c in raw:
                cookie = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".temu.com"),
                    "path": c.get("path", "/"),
                }
                # Optional fields — only add if present and valid
                if c.get("expirationDate"):
                    cookie["expires"] = int(c["expirationDate"])
                if "httpOnly" in c:
                    cookie["httpOnly"] = c["httpOnly"]
                if "secure" in c:
                    cookie["secure"] = c["secure"]
                if "sameSite" in c:
                    same = c["sameSite"]
                    if same in ("Strict", "Lax", "None"):
                        cookie["sameSite"] = same
                cookies.append(cookie)

            context.add_cookies(cookies)
            logger.info(f"[Temu] Injected {len(cookies)} cookies")
            return True

        except Exception as e:
            logger.warning(f"[Temu] Cookie load failed: {e}")
            return False

    # In TemuScraper only — NOT in BaseScraper

    def _fetch_playwright(self, url: str) -> Optional[str]:
        """Temu override: inject cookies before navigation."""
        try:
            from playwright.sync_api import sync_playwright
            import time, random

            fp = self._fingerprint()
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--window-size=1366,768",
                        "--disable-gpu",
                        "--lang=en-NG",
                    ],
                )
                context = browser.new_context(
                    user_agent=fp["user_agent"],
                    viewport={"width": 1366, "height": 768},
                    locale="en-NG",
                    timezone_id="Africa/Lagos",
                )
                context.add_init_script(self._stealth_js(fp))

                # Inject cookies BEFORE any navigation
                cookie_loaded = self._load_temu_cookies(context)
                if cookie_loaded:
                    logger.info("[Temu] Cookies injected — attempting direct search")

                page = context.new_page()
                page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}",
                    lambda r: r.abort(),
                )

                page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                time.sleep(3.0)
                html_lower = page.content().lower()


                if any(term in html_lower for term in ["challenge", "robot", "verify", "unusual traffic"]):
                    logger.error("[Temu] Playwright hit a hard Captcha/Security wall. Aborting.")
                    browser.close()
                    return None

                # If still on login page, try clicking Continue
                if "login" in page.url.lower():
                    logger.info("[Temu] Still on login page — trying Continue button")
                    for sel in ["button:has-text('Continue')", "[class*='continue']", "button[type='submit']"]:
                        try:
                            page.click(sel, timeout=2000)
                            time.sleep(2.0)
                            break
                        except Exception:
                            continue
                    # If click helped, navigate to search
                    if "login" not in page.url.lower():
                        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                        time.sleep(2.0)

                try:
                    page.wait_for_selector("a[href*='/goods'], [data-goods-id]", timeout=8_000)
                except Exception:
                    pass

                self._human_scroll(page)
                html = page.content()
                browser.close()

                has_products = "goods_id" in html.lower()
                logger.info(
                    f"[Temu] Playwright done. URL: {page.url[:80]}. Has goods_id: {has_products}. Size: {len(html)}")
                return html

        except Exception as e:
            logger.error(f"[Temu][Playwright] {e}")
            return None

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

        checks = [
            "login",
            "sign in",
            "verify",
            "human",
            "robot",
            "challenge",
            "security",
            "unusual traffic"
        ]

        for c in checks:
            print(c, c in html.lower())

        with open("temu_debug.html", "w", encoding="utf8") as f:
            f.write(html)
        print(repr(html[:200]))
        print(type(html))

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

        # ── Pattern 1: Standard __NEXT_DATA__ ─────────────────────
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                products = self._parse_goods_json(data, max_results)
                if products:
                    logger.info("[Temu] Extracted via __NEXT_DATA__")
                    return products
            except Exception as e:
                logger.debug(f"[Temu] __NEXT_DATA__ parse fail: {e}")

        # ── Pattern 2: window.__NEXT_DATA__ ───────────────────────
        m = re.search(r'window\.__NEXT_DATA__\s*=\s*({.*?})\s*;?\s*</script>', html, re.DOTALL)
        if m:
            try:
                products = self._parse_goods_json(json.loads(m.group(1)), max_results)
                if products:
                    return products
            except Exception:
                pass

        # ── Pattern 3: Temu inline script blobs (works on login redirect page) ──
        # Temu embeds search result state in multiple inline <script> blocks
        script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        for block in script_blocks:
            # Skip tiny blocks
            if len(block) < 100:
                continue
            # Look for JSON objects containing goods_id
            json_candidates = re.findall(r'\{[^{}]*"goods_id"[^{}]*\}', block)
            for candidate in json_candidates[:50]:
                try:
                    item = json.loads(candidate)
                    p = self._product_from_json(item)
                    if p:
                        # Found at least one — now get all from this block
                        all_in_block = re.findall(
                            r'\{(?:[^{}]|\{[^{}]*\})*"goods_id"(?:[^{}]|\{[^{}]*\})*\}',
                            block
                        )
                        products = []
                        for raw in all_in_block[:max_results]:
                            try:
                                obj = json.loads(raw)
                                prod = self._product_from_json(obj)
                                if prod:
                                    products.append(prod)
                            except Exception:
                                continue
                        if products:
                            logger.info(f"[Temu] Extracted {len(products)} via inline script block")
                            return products
                except Exception:
                    continue

        # ── Pattern 4: goodsList / goods_list arrays ───────────────
        for pattern in [
            r'"goodsList"\s*:\s*(\[.*?\])\s*[,}]',
            r'"goods_list"\s*:\s*(\[.*?\])\s*[,}]',
            r'"searchGoodsList"\s*:\s*(\[.*?\])\s*[,}]',
            r'"result"\s*:\s*\{[^}]*"goods"\s*:\s*(\[.*?\])',
            r'"data"\s*:\s*\{"goods"\s*:\s*(\[.*?\])',  # ← Temu login page pattern
            r'"goods"\s*:\s*(\[[^\[\]]{100,}\])',  # ← any goods array
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    items = json.loads(m.group(1))
                    products = self._parse_goods_json(items, max_results)
                    if products:
                        logger.info(f"[Temu] Extracted via pattern: {pattern[:40]}")
                        return products
                except Exception:
                    continue

        # ── Pattern 5: Extract goods_ids and build URLs (last resort) ──
        goods_ids = re.findall(r'"goods_id"\s*:\s*"?(\d+)"?', html)
        goods_ids = list(dict.fromkeys(goods_ids))[:max_results]  # deduplicated
        if goods_ids:
            logger.info(f"[Temu] Found {len(goods_ids)} goods_ids — building stub products")
            products = []
            # Try to get names paired with IDs
            for gid in goods_ids:
                # Look for goods_name near this goods_id
                pattern = rf'"goods_name"\s*:\s*"([^"]+)"[^{{}}]{{0,500}}"goods_id"\s*:\s*"?{gid}"?'
                name_m = re.search(pattern, html) or re.search(
                    rf'"goods_id"\s*:\s*"?{gid}"?[^{{}}]{{0,500}}"goods_name"\s*:\s*"([^"]+)"', html
                )
                title = name_m.group(1) if name_m else f"Temu Product {gid}"
                products.append(Product(
                    title=title,
                    source=self.SOURCE_NAME,
                    url=f"{TEMU_BASE}/goods.html?goods_id={gid}",
                    availability="in_stock",
                    fetched_via="goods_id_extraction",
                ))
            if products:
                return products

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
