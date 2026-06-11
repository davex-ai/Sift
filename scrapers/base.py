"""
scrapers/base.py

Changes from previous version:
  - ScraperAPI circuit breaker: disabled after 2 consecutive 403s/timeouts
  - Reduced ScraperAPI timeout 55s → 30s (fail fast)
  - BLOCK_IMAGES default True (subclasses can override)
  - Removed all debug print statements
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


@dataclass
class Product:
    title: str
    source: str
    url: str
    price_ngn: Optional[float] = None
    price_raw: str = ""
    currency: str = "NGN"
    rating: Optional[float] = None
    review_count: Optional[int] = None
    image_url: Optional[str] = None
    availability: str = "unknown"
    delivery_info: Optional[str] = None
    seller_name: Optional[str] = None
    location: Optional[str] = None
    condition: Optional[str] = None
    extra: dict = field(default_factory=dict)
    fetched_via: str = ""
    score: float = 0.0


class BaseScraper(ABC):

    SOURCE_NAME: str = "unknown"
    PROFILE: str = "generic"
    SELECTORS: dict = {}
    BLOCK_IMAGES: bool = True

    def __init__(self, scraperapi_key=None, use_playwright=True):
        self.scraperapi_key = scraperapi_key or config.SCRAPERAPI_KEY
        self.use_playwright = use_playwright
        self._limiter = RateLimiter(
            max_rpm=config.RATE_LIMIT_REQUESTS,
            max_concurrent=config.MAX_CONCURRENT_PER_SCRAPER,
            min_delay=config.MIN_DELAY_SECONDS,
            max_delay=config.MAX_DELAY_SECONDS,
            jitter=config.DELAY_JITTER,
        )
        self._cache: dict[str, list[Product]] = {}
        # Circuit breaker: skip ScraperAPI after repeated failures
        self._scraperapi_failures: int = 0
        self._scraperapi_disabled: bool = False

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[Product]: ...

    @abstractmethod
    def _parse_html(self, html: str, max_results: int) -> list[Product]: ...

    # ── Fetch chain ───────────────────────────────────────────

    def _fetch(self, url: str) -> Optional[str]:
        """
        ScraperAPI → Playwright → plain requests.
        Circuit breaker skips ScraperAPI after 2 consecutive failures,
        saving 30s × N scrapers on every search when key is dead.
        """
        if url in self._cache:
            logger.debug(f"[{self.SOURCE_NAME}] HTML cache hit")
            return self._cache[url]

        use_scraperapi = (
            bool(self.scraperapi_key)
            and not self._scraperapi_disabled
        )

        if use_scraperapi:
            html = self._fetch_scraperapi(url)
            if html:
                self._scraperapi_failures = 0
                self._cache[url] = html
                return html
            self._scraperapi_failures += 1
            if self._scraperapi_failures >= 2:
                self._scraperapi_disabled = True
                logger.warning(
                    f"[{self.SOURCE_NAME}] ScraperAPI circuit breaker tripped — "
                    "using Playwright for remainder of session. "
                    "Check https://app.scraperapi.com for credit status."
                )
            else:
                logger.warning(f"[{self.SOURCE_NAME}] ScraperAPI failed → Playwright")

        if self.use_playwright:
            html = self._fetch_playwright(url)
            if html:
                self._cache[url] = html
                return html
            logger.warning(f"[{self.SOURCE_NAME}] Playwright failed → plain requests")

        html = self._fetch_plain(url)
        if html:
            self._cache[url] = html
        return html

    def _fetch_with_retry(self, url: str) -> Optional[str]:
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
        logger.error(f"[{self.SOURCE_NAME}] All {config.MAX_RETRIES} attempts failed")
        return None

    # ── ScraperAPI ────────────────────────────────────────────

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
                timeout=30,  # Reduced from 55 — fail fast
            )
            if resp.status_code == 403:
                logger.error(
                    f"[{self.SOURCE_NAME}][ScraperAPI] 403 Forbidden — "
                    "key may be out of credits or invalid. "
                    "Check https://app.scraperapi.com"
                )
                return None
            resp.raise_for_status()
            if len(resp.text) < 500:
                logger.warning(f"[{self.SOURCE_NAME}][ScraperAPI] Response too short")
                return None
            logger.info(f"[{self.SOURCE_NAME}] ScraperAPI OK ({len(resp.text)} bytes)")
            return resp.text
        except requests.exceptions.Timeout:
            logger.error(f"[{self.SOURCE_NAME}][ScraperAPI] Timed out after 30s")
            return None
        except Exception as e:
            logger.error(f"[{self.SOURCE_NAME}][ScraperAPI] {e}")
            return None

    # ── Playwright ────────────────────────────────────────────

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

                time.sleep(random.uniform(0.3, 0.8))
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                wait_sel = self.SELECTORS.get("product_card", "body")
                if isinstance(wait_sel, list):
                    wait_sel = wait_sel[0]
                try:
                    page.wait_for_selector(wait_sel, timeout=10_000)
                except Exception:
                    pass

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

    def _human_scroll(self, page) -> None:
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
        fingerprints = [
            {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "platform": "Win32", "locale": "en-NG", "timezone": "Africa/Lagos"},
            {"user_agent": "Mozilla/5.0 (Linux; Android 13; SM-A155F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36", "platform": "Linux armv8l", "locale": "en-NG", "timezone": "Africa/Lagos"},
            {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0", "platform": "Win32", "locale": "en-NG", "timezone": "Africa/Lagos"},
        ]
        return random.choice(fingerprints)

    def _stealth_js(self, fp: dict) -> str:
        return f"""
        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
        Object.defineProperty(navigator, 'plugins', {{
            get: () => [{{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }}]
        }});
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            if (parameter === 37445) return 'Intel Open Source Technology Center';
            if (parameter === 37446) return 'Mesa DRI Intel(R) HD Graphics (SKL GT2)';
            return getParameter.call(this, parameter);
        }};
        window.chrome = {{ runtime: {{}} }};
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
            Promise.resolve({{ state: Notification.permission }}) :
            originalQuery(parameters)
        );
        Object.defineProperty(window, 'outerWidth', {{ get: () => 1366 }});
        Object.defineProperty(window, 'outerHeight', {{ get: () => 768 }});
        Object.defineProperty(navigator, 'platform', {{ get: () => '{fp["platform"]}' }});
        Object.defineProperty(screen, 'colorDepth', {{ get: () => 24 }});
        Object.defineProperty(screen, 'pixelDepth', {{ get: () => 24 }});
        Object.defineProperty(navigator, 'connection', {{
            get: () => ({{ effectiveType: '4g', rtt: 50, downlink: 10.0, saveData: false }})
        }});
        """

    def _fetch_plain(self, url: str) -> Optional[str]:
        try:
            resp = requests.get(url, headers=session_headers(self.SOURCE_NAME), timeout=20)
            resp.raise_for_status()
            logger.info(f"[{self.SOURCE_NAME}] Plain requests OK")
            return resp.text
        except Exception as e:
            logger.error(f"[{self.SOURCE_NAME}][Plain] {e}")
            return None

    @staticmethod
    def try_selectors(soup, selectors: list[str], attr: str = None):
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el.get(attr, "") if attr else el
        return None

    @staticmethod
    def try_selectors_all(soup, selectors: list[str]):
        for sel in selectors:
            results = soup.select(sel)
            if results:
                return results
        return []

    def validate_selectors(self, html: str) -> dict[str, bool]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        report = {}
        for name, sel in self.SELECTORS.items():
            found = bool(soup.select_one(sel)) if isinstance(sel, str) else any(soup.select_one(s) for s in sel)
            report[name] = found
        return report