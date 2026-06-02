"""
StealthBrowser — shared, hardened Playwright engine.

Every scraper (Jumia, Amazon, Konga, etc.) calls this.
No scraper manages its own browser — they just call:

    with StealthBrowser() as b:
        html = b.fetch(url, profile="amazon")

────────────────────────────────────────────────────────────────
WHY SITES DETECT BOTS — and what we do about each one:

  [1]  navigator.webdriver = true          → JS patch: set to undefined
  [2]  Zero plugins / mimeTypes            → JS patch: fake realistic plugins
  [3]  Headless Chrome signals in UA       → Launch args: remove HeadlessChrome
  [4]  Canvas fingerprint is identical     → JS patch: add imperceptible noise
  [5]  WebGL vendor = Google SwiftShader   → JS patch: spoof to Intel
  [6]  Robotic timing (too fast/uniform)   → Gaussian-jittered delays
  [7]  No real mouse movement              → Bezier-curve mouse paths
  [8]  Robotic scroll (instant full-page)  → Chunked scroll with pauses + backtrack
  [9]  Missing chrome.runtime object       → JS patch: inject full object
  [10] Permissions API returns 'denied'    → JS patch: realistic responses
  [11] outerWidth/Height = 0 (headless)    → JS patch: set realistic values
  [12] Consistent HTTP headers order       → Randomized per-session
  [13] Missing Sec-CH-UA headers           → Injected per UA
  [14] resource_type leaking automation    → Route intercept: abort fonts/media
  [15] navigator.languages inconsistency   → Patched to match UA locale
  [16] navigator.platform inconsistency    → Patched to match UA OS
  [17] AudioContext fingerprint            → JS patch: add noise to sample data
  [18] Date.getTimezoneOffset consistency  → timezone_id matched to locale
  [19] Screen colorDepth / pixelDepth      → JS patch: realistic 24-bit values
  [20] navigator.connection undefined      → JS patch: fake 4G connection

────────────────────────────────────────────────────────────────
PROVIDER PROFILES:
  Scrapers pass a profile hint so the engine applies site-specific
  extra hardening. Current profiles: "amazon", "jumia", "generic".
  Adding a new site? Add a profile entry, nothing else changes.

────────────────────────────────────────────────────────────────
USAGE:

    # Basic
    with StealthBrowser() as b:
        html = b.fetch("https://jumia.com.ng/...", profile="jumia")

    # Amazon (uses amazon profile — extra evasion)
    with StealthBrowser() as b:
        html = b.fetch("https://amazon.com/s?k=laptop", profile="amazon")

    # Reuse browser across multiple requests (saves startup time)
    browser = StealthBrowser()
    browser.start()
    html1 = browser.fetch(url1, profile="amazon")
    html2 = browser.fetch(url2, profile="amazon")
    browser.close()

    # With rotating proxy
    browser = StealthBrowser(proxy={
        "server": "http://proxy.host:8080",
        "username": "user",
        "password": "pass",
    })

    # Pagination / click navigation
    with StealthBrowser() as b:
        b.fetch(listing_url, profile="amazon")
        next_page_html = b.click_and_fetch("a.s-pagination-next")

    # Extract JS state (some sites embed data in window.__STATE__)
    with StealthBrowser() as b:
        data = b.fetch_js_variable(url, "window.__PRELOADED_STATE__")
"""

import re
import time
import random
import math
from typing import Optional, Callable, Any


# ══════════════════════════════════════════════════════════════════
# FINGERPRINT PROFILES
# Each profile describes a realistic browser + OS combo.
# One is picked randomly per session. Fields drive both launch args
# and JS patches so everything stays consistent.
# ══════════════════════════════════════════════════════════════════

_FINGERPRINTS = [
    {
        "id": "chrome124_win",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "platform": "Win32",
        "vendor": "Google Inc.",
        "locale": "en-US",
        "accept_language": "en-US,en;q=0.9",
        "timezone": "America/New_York",
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "hardware_concurrency": 8,
        "device_memory": 8,
        "screen_w": 1920, "screen_h": 1080,
        "viewport_w": 1440, "viewport_h": 900,
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "id": "chrome123_mac",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "platform": "MacIntel",
        "vendor": "Google Inc.",
        "locale": "en-US",
        "accept_language": "en-US,en;q=0.9",
        "timezone": "America/Los_Angeles",
        "sec_ch_ua": '"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"macOS"',
        "hardware_concurrency": 10,
        "device_memory": 16,
        "screen_w": 2560, "screen_h": 1600,
        "viewport_w": 1440, "viewport_h": 900,
        "webgl_vendor": "Apple Inc.",
        "webgl_renderer": "Apple M1 Pro",
    },
    {
        "id": "chrome124_mac",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "platform": "MacIntel",
        "vendor": "Google Inc.",
        "locale": "en-US",
        "accept_language": "en-US,en;q=0.9,fr;q=0.8",
        "timezone": "America/Chicago",
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"macOS"',
        "hardware_concurrency": 12,
        "device_memory": 16,
        "screen_w": 3024, "screen_h": 1964,
        "viewport_w": 1440, "viewport_h": 900,
        "webgl_vendor": "Apple Inc.",
        "webgl_renderer": "Apple M2 Pro",
    },
    {
        "id": "edge124_win",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
        ),
        "platform": "Win32",
        "vendor": "Google Inc.",
        "locale": "en-US",
        "accept_language": "en-US,en;q=0.9",
        "timezone": "America/Chicago",
        "sec_ch_ua": '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "hardware_concurrency": 16,
        "device_memory": 8,
        "screen_w": 1920, "screen_h": 1080,
        "viewport_w": 1440, "viewport_h": 900,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "id": "chrome124_win_4k",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "platform": "Win32",
        "vendor": "Google Inc.",
        "locale": "en-GB",
        "accept_language": "en-GB,en;q=0.9,en-US;q=0.8",
        "timezone": "Europe/London",
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "hardware_concurrency": 8,
        "device_memory": 16,
        "screen_w": 3840, "screen_h": 2160,
        "viewport_w": 1920, "viewport_h": 1080,
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
]


def _pick_fingerprint() -> dict:
    return random.choice(_FINGERPRINTS)


# ══════════════════════════════════════════════════════════════════
# SITE PROFILES
# Extra behavior settings per site. Scrapers pass profile="amazon"
# ══════════════════════════════════════════════════════════════════

_SITE_PROFILES = {
    "amazon": {
        "wait_until": "domcontentloaded",
        "extra_wait_ms": 2000,           # Amazon loads products async
        "abort_resources": {"image", "media", "font"},
        "scroll_depth": 0.6,             # Scroll 60% of page
        "pre_navigate_delay": (2.0, 4.5),
        "post_load_delay": (1.5, 3.0),
        "block_signals": [
            "Type the characters you see",
            "api-services-support@amazon.com",
            "Enter the characters you see below",
            "Sorry, we just need to make sure you",
        ],
        "retry_selector": None,          # No extra selector wait
    },
    "jumia": {
        "wait_until": "domcontentloaded",
        "extra_wait_ms": 1000,
        "abort_resources": {"image", "media", "font"},
        "scroll_depth": 0.5,
        "pre_navigate_delay": (1.0, 2.5),
        "post_load_delay": (0.8, 1.8),
        "block_signals": [
            "Access denied",
            "blocked",
        ],
        "retry_selector": None,
    },
    "generic": {
        "wait_until": "domcontentloaded",
        "extra_wait_ms": 500,
        "abort_resources": {"media", "font"},
        "scroll_depth": 0.4,
        "pre_navigate_delay": (1.0, 2.5),
        "post_load_delay": (0.5, 1.5),
        "block_signals": [
            "Access denied",
            "Robot or human?",
            "Checking your browser",
            "DDoS protection",
            "Enable JavaScript and cookies",
            "Please verify you are a human",
            "_pxCaptcha",
            "datadome",
        ],
        "retry_selector": None,
    },
}

# Universal block signals checked on every request regardless of profile
_UNIVERSAL_BLOCK_SIGNALS = [
    "Robot or human?",
    "Checking your browser",
    "DDoS protection by",
    "Enable JavaScript and cookies",
    "Please verify you are a human",
    "_pxCaptcha",
    "px-captcha",
    "datadome",
    "cf-browser-verification",
    "challenge-running",
]


# ══════════════════════════════════════════════════════════════════
# JS PATCHES
# Injected via add_init_script — runs before any page JS.
# Template: fill in fingerprint values at runtime.
# ══════════════════════════════════════════════════════════════════

def _build_js_patches(fp: dict) -> str:
    """Build JS patch string from a fingerprint dict."""
    # Audio noise seed — random per session so AudioContext fingerprint varies
    audio_noise = random.uniform(0.0001, 0.0003)
    canvas_noise = random.randint(0, 9)

    return f"""
// ── [1] Remove webdriver flag ─────────────────────────────────
Object.defineProperty(navigator, 'webdriver', {{
    get: () => undefined, configurable: true
}});

// ── [2] Platform consistency ───────────────────────────────────
Object.defineProperty(navigator, 'platform', {{
    get: () => '{fp["platform"]}', configurable: true
}});

// ── [3] Vendor ─────────────────────────────────────────────────
Object.defineProperty(navigator, 'vendor', {{
    get: () => '{fp["vendor"]}', configurable: true
}});

// ── [4] Language consistency ───────────────────────────────────
Object.defineProperty(navigator, 'language', {{
    get: () => '{fp["locale"]}', configurable: true
}});
Object.defineProperty(navigator, 'languages', {{
    get: () => {repr(fp["accept_language"].split(',')[0:2])}, configurable: true
}});

// ── [5] Hardware concurrency ───────────────────────────────────
Object.defineProperty(navigator, 'hardwareConcurrency', {{
    get: () => {fp["hardware_concurrency"]}, configurable: true
}});

// ── [6] Device memory ──────────────────────────────────────────
Object.defineProperty(navigator, 'deviceMemory', {{
    get: () => {fp["device_memory"]}, configurable: true
}});

// ── [7] Screen dimensions ──────────────────────────────────────
Object.defineProperty(screen, 'width',       {{ get: () => {fp["screen_w"]} }});
Object.defineProperty(screen, 'height',      {{ get: () => {fp["screen_h"]} }});
Object.defineProperty(screen, 'availWidth',  {{ get: () => {fp["screen_w"]} }});
Object.defineProperty(screen, 'availHeight', {{ get: () => {fp["screen_h"] - 40} }});
Object.defineProperty(screen, 'colorDepth',  {{ get: () => 24 }});
Object.defineProperty(screen, 'pixelDepth',  {{ get: () => 24 }});

// ── [8] outerWidth/Height (headless = 0) ──────────────────────
if (window.outerWidth === 0) {{
    Object.defineProperty(window, 'outerWidth',  {{ get: () => window.innerWidth  + 16 }});
    Object.defineProperty(window, 'outerHeight', {{ get: () => window.innerHeight + 88 }});
}}

// ── [9] Fake plugins (headless has 0) ─────────────────────────
Object.defineProperty(navigator, 'plugins', {{
    get: () => {{
        const makePlugin = (name, filename, desc, type, suffixes) => {{
            const plugin = Object.create(Plugin.prototype);
            const mt = Object.create(MimeType.prototype, {{
                type:          {{ value: type,     enumerable: true }},
                suffixes:      {{ value: suffixes,  enumerable: true }},
                description:   {{ value: desc,      enumerable: true }},
                enabledPlugin: {{ value: plugin,    enumerable: true }}
            }});
            Object.defineProperties(plugin, {{
                name:        {{ value: name,     enumerable: true }},
                filename:    {{ value: filename,  enumerable: true }},
                description: {{ value: desc,      enumerable: true }},
                length:      {{ value: 1,         enumerable: true }},
                0:           {{ value: mt,        enumerable: true }},
                item:        {{ value: (i) => i === 0 ? mt : null }},
                namedItem:   {{ value: (n) => n === type ? mt : null }},
            }});
            return plugin;
        }};
        const plugins = [
            makePlugin('Chrome PDF Plugin','internal-pdf-viewer','Portable Document Format','application/x-google-chrome-pdf','pdf'),
            makePlugin('Chrome PDF Viewer','mhjfbmdgcfjbbpaeojofohoefgiehjai','','application/pdf','pdf'),
            makePlugin('Native Client','internal-nacl-plugin','','application/x-nacl',''),
        ];
        const arr = Object.create(PluginArray.prototype);
        plugins.forEach((p, i) => arr[i] = p);
        Object.defineProperty(arr, 'length', {{ value: plugins.length }});
        arr[Symbol.iterator] = Array.prototype[Symbol.iterator];
        arr.item = (i) => plugins[i] || null;
        arr.namedItem = (n) => plugins.find(p => p.name === n) || null;
        arr.refresh = () => {{}};
        return arr;
    }}
}});

// ── [10] Chrome runtime (missing in headless) ─────────────────
if (!window.chrome) {{
    window.chrome = {{
        app: {{
            isInstalled: false,
            InstallState: {{ DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed' }},
            RunningState: {{ CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running' }},
            getDetails: () => null, getIsInstalled: () => false,
        }},
        csi: () => ({{ startE: Date.now(), onloadT: Date.now() + 200, pageT: 2000, tran: 15 }}),
        loadTimes: () => ({{
            commitLoadTime: Date.now()/1000 - 1,
            connectionInfo: 'h2',
            finishDocumentLoadTime: Date.now()/1000 - 0.2,
            finishLoadTime: Date.now()/1000 - 0.1,
            firstPaintAfterLoadTime: 0,
            firstPaintTime: Date.now()/1000 - 0.5,
            navigationType: 'Other',
            npnNegotiatedProtocol: 'h2',
            requestTime: Date.now()/1000 - 2,
            startLoadTime: Date.now()/1000 - 1.8,
            wasAlternateProtocolAvailable: false,
            wasFetchedViaSpdy: true,
            wasNpnNegotiated: true,
        }}),
        runtime: {{
            OnInstalledReason: {{ CHROME_UPDATE:'chrome_update', INSTALL:'install', SHARED_MODULE_UPDATE:'shared_module_update', UPDATE:'update' }},
            PlatformArch: {{ ARM:'arm', ARM64:'arm64', MIPS:'mips', MIPS64:'mips64', X86_32:'x86-32', X86_64:'x86-64' }},
            PlatformOs: {{ ANDROID:'android', CROS:'cros', LINUX:'linux', MAC:'mac', OPENBSD:'openbsd', WIN:'win' }},
            id: undefined,
        }},
    }};
}}

// ── [11] WebGL vendor/renderer spoofing ───────────────────────
const _patchWebGL = (ctx) => {{
    const orig = ctx.prototype.getParameter;
    ctx.prototype.getParameter = function(p) {{
        if (p === 37445) return '{fp["webgl_vendor"]}';
        if (p === 37446) return '{fp["webgl_renderer"]}';
        return orig.call(this, p);
    }};
}};
_patchWebGL(WebGLRenderingContext);
if (typeof WebGL2RenderingContext !== 'undefined') _patchWebGL(WebGL2RenderingContext);

// ── [12] Canvas noise ─────────────────────────────────────────
const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(...args) {{
    const data = _origToDataURL.apply(this, args);
    return data.length > 100
        ? data.slice(0, -3) + String.fromCharCode(48 + {canvas_noise})
        : data;
}};
const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
    const d = _origGetImageData.apply(this, args);
    for (let i = 0; i < d.data.length; i += 100) {{
        d.data[i] = d.data[i] ^ ({canvas_noise});
    }}
    return d;
}};

// ── [13] AudioContext fingerprint noise ───────────────────────
const _AudioContext = window.AudioContext || window.webkitAudioContext;
if (_AudioContext) {{
    const _origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(...args) {{
        const arr = _origGetChannelData.apply(this, args);
        for (let i = 0; i < arr.length; i += 100) {{
            arr[i] += {audio_noise};
        }}
        return arr;
    }};
}}

// ── [14] Permissions API ──────────────────────────────────────
if (navigator.permissions && navigator.permissions.query) {{
    const _origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({{ state: Notification.permission }})
            : _origQuery(p);
}}

// ── [15] Connection type ──────────────────────────────────────
if (!navigator.connection) {{
    Object.defineProperty(navigator, 'connection', {{
        get: () => ({{ effectiveType:'4g', rtt:50, downlink:10, saveData:false,
                        addEventListener:()=>{{}}, removeEventListener:()=>{{}} }})
    }});
}}

// ── [16] Battery API ──────────────────────────────────────────
if (navigator.getBattery) {{
    navigator.getBattery = () => Promise.resolve({{
        charging:true, chargingTime:0, dischargingTime:Infinity, level:1.0,
        addEventListener:()=>{{}}, removeEventListener:()=>{{}}
    }});
}}

// ── [17] iframe contentWindow ─────────────────────────────────
try {{
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {{
        get: function() {{ return window; }}
    }});
}} catch(e) {{}}

// ── [18] Notification.permission (headless = 'denied' always) ─
try {{
    Object.defineProperty(Notification, 'permission', {{
        get: () => 'default', configurable: true
    }});
}} catch(e) {{}}

// ── [19] Object.getOwnPropertyDescriptor for webdriver ────────
// Some checks use this instead of direct access
const _origGetOPD = Object.getOwnPropertyDescriptor;
Object.getOwnPropertyDescriptor = function(obj, key) {{
    if (obj === navigator && key === 'webdriver') return undefined;
    return _origGetOPD(obj, key);
}};

console.debug('[stealth] Patches applied — fingerprint: {fp["id"]}');
"""


# ══════════════════════════════════════════════════════════════════
# HUMAN SIMULATION UTILS
# ══════════════════════════════════════════════════════════════════

def _bezier(p0, p1, p2, p3, t):
    """Cubic bezier point at t."""
    mt = 1 - t
    return (
        mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0],
        mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1],
    )


def _human_path(start, end, steps=30):
    """Generate human-like curved mouse path between two points."""
    dx, dy = end[0]-start[0], end[1]-start[1]
    cp1 = (start[0] + dx*0.2 + random.randint(-80, 80),
           start[1] + dy*0.2 + random.randint(-80, 80))
    cp2 = (start[0] + dx*0.8 + random.randint(-80, 80),
           start[1] + dy*0.8 + random.randint(-80, 80))
    return [_bezier(start, cp1, cp2, end, i/steps) for i in range(steps+1)]


# ══════════════════════════════════════════════════════════════════
# MAIN CLASS
# ══════════════════════════════════════════════════════════════════

class StealthBrowser:
    """
    Hardened shared Playwright engine.
    All scrapers use this — they never manage their own browser.

    See module docstring for full usage examples.
    """

    def __init__(
        self,
        headless: bool = True,
        proxy: Optional[dict] = None,
        timeout_ms: int = 35_000,
        max_retries: int = 3,
        debug: bool = False,
    ):
        """
        Args:
            headless:    True for production, False for debugging (see browser)
            proxy:       {"server": "http://host:port", "username": ..., "password": ...}
            timeout_ms:  Page load timeout
            max_retries: Retries on block detection (exponential backoff)
            debug:       Verbose logging
        """
        self.headless = headless
        self.proxy = proxy
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.debug = debug

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._fp: Optional[dict] = None  # Active fingerprint

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> "StealthBrowser":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._launch()
        return self

    def close(self):
        try:
            if self._browser: self._browser.close()
        except Exception: pass
        try:
            if self._pw: self._pw.stop()
        except Exception: pass
        self._pw = self._browser = self._context = self._page = self._fp = None

    def __enter__(self): return self.start()
    def __exit__(self, *_): self.close()

    # ── Public API (what scrapers call) ──────────────────────────

    def fetch(
        self,
        url: str,
        profile: str = "generic",
        wait_for: Optional[str] = None,
        on_ready: Optional[Callable] = None,
        _retry: int = 0,
    ) -> Optional[str]:
        """
        Fetch URL and return HTML string. Returns None if all retries fail.

        Args:
            url:       Target URL
            profile:   Site profile key — "amazon", "jumia", or "generic"
            wait_for:  CSS selector to wait for after page load
            on_ready:  Optional callback fn(page) called after load
            _retry:    Internal retry counter, don't pass this
        """
        if not self._pw:
            self.start()

        cfg = _SITE_PROFILES.get(profile, _SITE_PROFILES["generic"])

        try:
            # Pre-navigation human delay
            self._delay(*cfg["pre_navigate_delay"])

            resp = self._page.goto(
                url,
                wait_until=cfg["wait_until"],
                timeout=self.timeout_ms,
            )

            # Hard HTTP block
            if resp and resp.status in (403, 429, 503):
                self._log(f"HTTP {resp.status} on {url}")
                return self._retry(url, profile, wait_for, on_ready, _retry, "http_block")

            # Wait for extra JS rendering
            if cfg["extra_wait_ms"]:
                self._page.wait_for_timeout(cfg["extra_wait_ms"])

            # Optional: wait for specific element
            if wait_for:
                try:
                    self._page.wait_for_selector(wait_for, timeout=12_000)
                except Exception:
                    self._log(f"wait_for selector '{wait_for}' timed out")

            # Simulate human scroll
            self._scroll(cfg["scroll_depth"])

            # Post-load delay
            self._delay(*cfg["post_load_delay"])

            # Optional custom hook
            if on_ready:
                on_ready(self._page)

            html = self._page.content()

            # Check for block/challenge signals
            block_signals = list(_UNIVERSAL_BLOCK_SIGNALS) + cfg.get("block_signals", [])
            if self._is_blocked(html, block_signals):
                self._log(f"Block signal in HTML for {url}")
                return self._retry(url, profile, wait_for, on_ready, _retry, "content_block")

            self._log(f"OK — {len(html):,} bytes from {url[:60]}")
            return html

        except Exception as e:
            self._log(f"fetch() exception: {e}")
            return self._retry(url, profile, wait_for, on_ready, _retry, str(e))

    def fetch_js_variable(self, url: str, js_expr: str, profile: str = "generic") -> Any:
        """
        Fetch a page and evaluate a JS expression, returning its value.
        Useful when product data is embedded in window.__STATE__ or similar.

        Example:
            data = browser.fetch_js_variable(url, "JSON.stringify(window.__REDUX_STATE__)")
        """
        html = self.fetch(url, profile=profile)
        if not html:
            return None
        try:
            return self._page.evaluate(js_expr)
        except Exception as e:
            self._log(f"fetch_js_variable eval error: {e}")
            return None

    def click_and_fetch(
        self,
        selector: str,
        wait_for: Optional[str] = None,
        timeout_ms: int = 10_000,
    ) -> Optional[str]:
        """
        Move mouse naturally to selector, click it, wait, return new HTML.
        Use for pagination, "Load more" buttons, filters, etc.
        """
        if not self._page:
            self._log("click_and_fetch called before fetch()")
            return None
        try:
            el = self._page.query_selector(selector)
            if not el:
                self._log(f"click_and_fetch: '{selector}' not found")
                return None

            box = el.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                self._move_mouse(cx, cy)

            self._delay(0.2, 0.6)
            el.click()
            self._delay(0.4, 1.2)

            if wait_for:
                self._page.wait_for_selector(wait_for, timeout=timeout_ms)
            else:
                try:
                    self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except Exception:
                    pass

            return self._page.content()
        except Exception as e:
            self._log(f"click_and_fetch error: {e}")
            return None

    def current_url(self) -> Optional[str]:
        """Return the current page URL (useful after redirects)."""
        try:
            return self._page.url if self._page else None
        except Exception:
            return None

    # ── Internal ─────────────────────────────────────────────────

    def _launch(self):
        self._fp = _pick_fingerprint()
        self._log(f"Fingerprint: {self._fp['id']}")

        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            proxy=self.proxy,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process,UserAgentClientHint",
                "--disable-site-isolation-trials",
                "--disable-web-security",
                "--allow-running-insecure-content",
                "--disable-notifications",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-extensions",
                f"--window-size={self._fp['screen_w']},{self._fp['screen_h']}",
                "--use-gl=swiftshader",  # Reduces headless detection via GPU fingerprint
                "--disable-ipc-flooding-protection",
                "--password-store=basic",
                "--use-mock-keychain",
            ],
        )

        self._context = self._browser.new_context(
            user_agent=self._fp["user_agent"],
            viewport={"width": self._fp["viewport_w"], "height": self._fp["viewport_h"]},
            screen={"width": self._fp["screen_w"], "height": self._fp["screen_h"]},
            locale=self._fp["locale"],
            timezone_id=self._fp["timezone"],
            java_script_enabled=True,
            accept_downloads=False,
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": self._fp["accept_language"],
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "sec-ch-ua": self._fp["sec_ch_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": self._fp["sec_ch_ua_platform"],
                "Cache-Control": "max-age=0",
            },
        )

        # Inject all JS patches before any page script runs
        self._context.add_init_script(_build_js_patches(self._fp))

        self._page = self._context.new_page()

        # Abort resources that waste bandwidth and can trigger tracking
        # but keep JS (needed for rendering) and XHR/fetch (product data)
        self._page.route("**/*", self._route_handler)

    def _route_handler(self, route):
        """Abort unnecessary resource types to save bandwidth + reduce fingerprint."""
        rtype = route.request.resource_type
        url = route.request.url

        # Never abort: document, script, xhr, fetch, websocket
        # These carry product data and page structure
        if rtype in ("document", "script", "xhr", "fetch", "websocket"):
            route.continue_()
            return

        # Abort: images (except we want them for scraperapi fallback check),
        # media, fonts — but allow stylesheet (needed for layout)
        if rtype in ("image", "media", "font", "other"):
            # Allow captcha images through — important for detection
            if "captcha" in url.lower() or "challenge" in url.lower():
                route.continue_()
            else:
                route.abort()
            return

        route.continue_()

    def _relaunch(self):
        """Close browser and relaunch with fresh fingerprint."""
        self._log("Relaunching with fresh fingerprint...")
        try:
            if self._browser: self._browser.close()
        except Exception: pass
        self._launch()

    def _retry(self, url, profile, wait_for, on_ready, retry_count, reason):
        if retry_count >= self.max_retries:
            self._log(f"Max retries ({self.max_retries}) reached. Giving up.")
            return None

        backoff = (2 ** retry_count) + random.uniform(1, 3)
        self._log(f"Retry {retry_count+1}/{self.max_retries} in {backoff:.1f}s (reason: {reason})")
        time.sleep(backoff)
        self._relaunch()
        return self.fetch(url, profile, wait_for, on_ready, _retry=retry_count+1)

    def _is_blocked(self, html: str, signals: list) -> bool:
        lower = html.lower()
        return any(s.lower() in lower for s in signals)

    def _scroll(self, depth_ratio: float = 0.5):
        """Scroll to depth_ratio of page height in human-like chunks."""
        try:
            total = self._page.evaluate("document.body.scrollHeight")
            target = int(total * depth_ratio)
            current = 0

            while current < target:
                chunk = random.randint(250, 550)
                current = min(current + chunk, target)
                self._page.evaluate(
                    f"window.scrollTo({{top:{current}, behavior:'smooth'}})"
                )
                self._delay(0.25, 0.9)

                # Occasional micro-backtrack (very human)
                if random.random() < 0.12:
                    back = random.randint(40, 150)
                    current = max(0, current - back)
                    self._page.evaluate(
                        f"window.scrollTo({{top:{current}, behavior:'smooth'}})"
                    )
                    self._delay(0.1, 0.4)

        except Exception:
            pass  # Non-fatal

    def _move_mouse(self, tx: float, ty: float):
        """Move mouse along bezier path to (tx, ty)."""
        try:
            sx = random.randint(300, 900)
            sy = random.randint(200, 600)
            steps = random.randint(20, 35)
            for pt in _human_path((sx, sy), (tx, ty), steps):
                self._page.mouse.move(pt[0], pt[1])
                time.sleep(random.uniform(0.004, 0.018))
        except Exception:
            pass

    def _delay(self, min_s: float, max_s: float):
        """Gaussian-jittered delay — more human than uniform."""
        base = random.uniform(min_s, max_s)
        jitter = random.gauss(0, (max_s - min_s) * 0.1)
        time.sleep(max(0.05, base + jitter))

    def _log(self, msg: str):
        if self.debug:
            print(f"[StealthBrowser] {msg}")


# ══════════════════════════════════════════════════════════════════
# STANDALONE TEST
# Run: python -m shopping_scraper.utils.stealth_browser [url] [profile]
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from bs4 import BeautifulSoup

    url     = sys.argv[1] if len(sys.argv) > 1 else "https://www.amazon.com/s?k=laptop&crid=2097QGMOY912R&sprefix=lapt%2Caps%2C1117&ref=nb_sb_noss_2"
    profile = sys.argv[2] if len(sys.argv) > 2 else "amazon"

    print(f"\nTesting StealthBrowser")
    print(f"  URL:     {url}")
    print(f"  Profile: {profile}\n")

    with StealthBrowser(headless=True, debug=True) as browser:
        html = browser.fetch(url, profile=profile)

    if html:
        soup = BeautifulSoup(html, "html.parser")
        print(f"\n✅ Success — {len(html):,} bytes")
        # Quick check
        if profile == "amazon":
            cards = soup.select("div[data-component-type='s-search-result']")
            print(f"   Product cards found: {len(cards)}")
        elif profile == "jumia":
            cards = soup.select("article.prd")
            print(f"   Product cards found: {len(cards)}")
        titles = [t.get_text(strip=True)[:60] for t in soup.find_all(["h2","h3"])[:3]]
        for t in titles:
            print(f"   · {t}")
    else:
        print("\n❌ Failed — no HTML returned")