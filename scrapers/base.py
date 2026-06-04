"""
BaseScraper — plugin contract every store scraper inherits.

Implements:
  - ScraperAPI → Playwright → cached result fallback chain
  - Token-bucket rate limiting per scraper
  - Randomized delays + concurrency cap
  - Exponential backoff retry
  - Selector-check helper

To add a new store scraper:
  1. Subclass BaseScraper
  2. Set SOURCE_NAME and SELECTORS
  3. Implement search(), _parse_html()
  4. Register in scrapers/__init__.py
  Nothing else changes.
"""

import time
import random
import logging
import requests
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

from utils.rate_limiter import RateLimiter, global_limiter
from utils.headers import session_headers
import config

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════

@dataclass
class Product:
    """Raw product from a single scraper — before normalization."""
    title: str
    source: str                         # 'jumia', 'konga', etc.
    url: str

    price_ngn: Optional[float] = None
    price_raw: str = ""
    currency: str = "NGN"

    rating: Optional[float] = None
    review_count: Optional[int] = None

    image_url: Optional[str] = None
    availability: str = "unknown"       # 'in_stock' | 'out_of_stock' | 'unknown'
    delivery_info: Optional[str] = None
    seller_name: Optional[str] = None
    location: Optional[str] = None      # Jiji / marketplace listings
    condition: Optional[str] = None     # 'new' | 'used' | 'refurbished'

    extra: dict = field(default_factory=dict)

    # Internal — set by pipeline
    fetched_via: str = ""               # 'scraperapi' | 'playwright' | 'cache'
    score: float = 0.0


# ══════════════════════════════════════════════════════════════
# Base scraper
# ══════════════════════════════════════════════════════════════

class BaseScraper(ABC):
    """
    Plugin base class.  Subclasses implement search() and _parse_html().
    All rate limiting, fallback chain, and retry logic lives here.
    """

    SOURCE_NAME: str = "unknown"        # override in subclass
    PROFILE: str = "generic"           # stealth profile ('jumia', 'konga', etc.)

    # Selectors dict — override in subclass
    SELECTORS: dict = {}

    def __init__(
        self,
        scraperapi_key: Optional[str] = None,
        use_playwright: bool = True,
    ):
        self.scraperapi_key = scraperapi_key or config.SCRAPERAPI_KEY
        self.use_playwright = use_playwright

        # Per-scraper rate limiter
        self._limiter = RateLimiter(
            max_rpm=config.RATE_LIMIT_REQUESTS,
            max_concurrent=config.MAX_CONCURRENT_PER_SCRAPER,
            min_delay=config.MIN_DELAY_SECONDS,
            max_delay=config.MAX_DELAY_SECONDS,
            jitter=config.DELAY_JITTER,
        )

        # Last N results cached for instant return on repeated query
        self._cache: dict[str, list[Product]] = {}

    # ── Public interface ───────────────────────────────────────

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[Product]:
        """Search this store. Returns raw Product list."""
        ...

    @abstractmethod
    def _parse_html(self, html: str, max_results: int) -> list[Product]:
        """Parse raw HTML → Products. Implemented per store."""
        ...

    # ── Fetch chain ───────────────────────────────────────────

    def _fetch(self, url: str) -> Optional[str]:
        """
        Fallback chain:
          1. ScraperAPI (with JS rendering)
          2. Playwright stealth
          3. Plain requests (last resort)
        Returns HTML string or None.
        """
        # Check cache first
        if url in self._cache:
            logger.debug(f"[{self.SOURCE_NAME}] HTML cache hit")
            return self._cache[url]

        # Try ScraperAPI
        if self.scraperapi_key:
            html = self._fetch_scraperapi(url)
            if html:
                self._cache[url] = html
                return html
            logger.warning(f"[{self.SOURCE_NAME}] ScraperAPI failed → Playwright")

        # Try Playwright
        if self.use_playwright:
            html = self._fetch_playwright(url)
            if html:
                self._cache[url] = html
                return html
            logger.warning(f"[{self.SOURCE_NAME}] Playwright failed → plain requests")

        # Plain requests (no stealth — last resort)
        html = self._fetch_plain(url)
        if html:
            self._cache[url] = html
        return html

    def _fetch_with_retry(self, url: str) -> Optional[str]:
        """Wrap _fetch with exponential backoff retries."""
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                with global_limiter.limit():
                    with self._limiter.throttle():
                        html = self._fetch(url)
                if html:
                    return html
            except Exception as e:
                logger.error(f"[{self.SOURCE_NAME}] Attempt {attempt} error: {e}")

            if attempt < config.MAX_RETRIES:
                sleep_time = config.RETRY_BACKOFF ** attempt + random.uniform(0, 1)
                logger.info(f"[{self.SOURCE_NAME}] Retrying in {sleep_time:.1f}s...")
                time.sleep(sleep_time)

        logger.error(f"[{self.SOURCE_NAME}] All {config.MAX_RETRIES} attempts failed for {url}")
        return None

    # ── ScraperAPI ─────────────────────────────────────────────

    def _scraperapi_url(self, target_url: str, render_js: bool = True, **extra) -> str:
        params = {
            "api_key": self.scraperapi_key,
            "url": target_url,
            "render": "true" if render_js else "false",
            "country_code": "ng",
            "device_type": "desktop",
        }
        params.update(extra)
        return f"{config.SCRAPERAPI_BASE}?{urlencode(params)}"

    def _fetch_scraperapi(self, url: str) -> Optional[str]:
        try:
            api_url = self._scraperapi_url(url)
            resp = requests.get(
                api_url,
                headers=session_headers(self.SOURCE_NAME),
                timeout=35,
            )
            resp.raise_for_status()
            if len(resp.text) < 500:
                logger.warning(f"[{self.SOURCE_NAME}][ScraperAPI] Response too short")
                return None
            logger.info(f"[{self.SOURCE_NAME}] ScraperAPI OK ({len(resp.text)} bytes)")
            return resp.text
        except Exception as e:
            logger.error(f"[{self.SOURCE_NAME}][ScraperAPI] {e}")
            return None

    # ── Playwright ─────────────────────────────────────────────

    def _fetch_playwright(self, url: str) -> Optional[str]:
        """
        Stealth Playwright fetch.
        Applies all 20 anti-detection patches from stealth profile.
        """
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

                # Inject anti-detection JS
                context.add_init_script(self._stealth_js(fp))

                page = context.new_page()

                # Block heavy resources
                page.route(
                    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,otf,eot}",
                    lambda r: r.abort(),
                )

                # Simulate human arrival
                time.sleep(random.uniform(0.3, 0.8))
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                # Wait for product content (best-effort)
                wait_sel = self.SELECTORS.get("product_card", "body")
                try:
                    page.wait_for_selector(wait_sel, timeout=10_000)
                except Exception:
                    pass  # continue anyway

                # Subtle scroll to trigger lazy images
                self._human_scroll(page)

                html = page.content()
                browser.close()
                logger.info(f"[{self.SOURCE_NAME}] Playwright OK ({len(html)} bytes)")
                return html

        except ImportError:
            logger.error("[BaseScraper] Playwright not installed. pip install playwright && playwright install chromium")
            return None
        except Exception as e:
            logger.error(f"[{self.SOURCE_NAME}][Playwright] {e}")
            return None

    def _human_scroll(self, page) -> None:
        """Scroll down in human-like chunks."""
        try:
            height = page.evaluate("document.body.scrollHeight")
            step = random.randint(300, 600)
            pos = 0
            while pos < height:
                pos = min(pos + step, height)
                page.evaluate(f"window.scrollTo(0, {pos})")
                time.sleep(random.uniform(0.1, 0.4))
        except Exception:
            pass

    def _fingerprint(self) -> dict:
        """Pick a random realistic fingerprint."""
        fingerprints = [
            {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "platform": "Win32", "locale": "en-NG", "timezone": "Africa/Lagos"},
            {"user_agent": "Mozilla/5.0 (Linux; Android 13; SM-A155F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36", "platform": "Linux armv8l", "locale": "en-NG", "timezone": "Africa/Lagos"},
            {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0", "platform": "Win32", "locale": "en-NG", "timezone": "Africa/Lagos"},
        ]
        return random.choice(fingerprints)

    def _stealth_js(self, fp: dict) -> str:
        """Anti-detection JS patches injected before page load."""
        return f"""
        // [1] Remove webdriver flag
        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});

        // [2] Fake plugins
        Object.defineProperty(navigator, 'plugins', {{
            get: () => [{{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }}]
        }});

        // [5] WebGL vendor spoofing
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            if (parameter === 37445) return 'Intel Open Source Technology Center';
            if (parameter === 37446) return 'Mesa DRI Intel(R) HD Graphics (SKL GT2)';
            return getParameter.call(this, parameter);
        }};

        // [9] chrome.runtime
        window.chrome = {{ runtime: {{}} }};

        // [10] Permissions API
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({{ state: Notification.permission }}) :
            originalQuery(parameters)
        );

        // [11] Screen dimensions
        Object.defineProperty(window, 'outerWidth', {{ get: () => 1366 }});
        Object.defineProperty(window, 'outerHeight', {{ get: () => 768 }});

        // [15] navigator.platform
        Object.defineProperty(navigator, 'platform', {{ get: () => '{fp["platform"]}' }});

        // [19] colorDepth
        Object.defineProperty(screen, 'colorDepth', {{ get: () => 24 }});
        Object.defineProperty(screen, 'pixelDepth', {{ get: () => 24 }});

        // [20] Connection API
        Object.defineProperty(navigator, 'connection', {{
            get: () => ({{ effectiveType: '4g', rtt: 50, downlink: 10.0, saveData: false }})
        }});
        """

    # ── Plain requests fallback ────────────────────────────────

    def _fetch_plain(self, url: str) -> Optional[str]:
        """Bare requests — no stealth. Last resort."""
        try:
            resp = requests.get(
                url,
                headers=session_headers(self.SOURCE_NAME),
                timeout=20,
            )
            resp.raise_for_status()
            logger.info(f"[{self.SOURCE_NAME}] Plain requests OK")
            return resp.text
        except Exception as e:
            logger.error(f"[{self.SOURCE_NAME}][Plain] {e}")
            return None

    # ── Selector helpers ───────────────────────────────────────

    @staticmethod
    def try_selectors(soup, selectors: list[str], attr: str = None):
        """
        Try multiple CSS selectors, return the first match.
        If attr is given, returns that attribute value instead of the element.
        """
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                if attr:
                    return el.get(attr, "")
                return el
        return None

    @staticmethod
    def try_selectors_all(soup, selectors: list[str]):
        """Try multiple CSS selectors for a list, return first non-empty."""
        for sel in selectors:
            results = soup.select(sel)
            if results:
                return results
        return []

    def validate_selectors(self, html: str) -> dict[str, bool]:
        """
        Check which selectors are still working.
        Run this in selector_check.py to detect broken scrapers.
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        report = {}
        for name, sel in self.SELECTORS.items():
            found = bool(soup.select_one(sel)) if isinstance(sel, str) else any(soup.select_one(s) for s in sel)
            report[name] = found
        return report
