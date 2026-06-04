"""
Random headers & user-agent rotation.
Keeps a realistic browser fingerprint on every request.
"""

import random

# ── Realistic Chrome UAs (Nigeria-relevant OS mix) ──────────
_USER_AGENTS = [
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome / Android (very common in Nigeria)
    "Mozilla/5.0 (Linux; Android 13; SM-A155F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Redmi Note 10 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.99 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; TECNO KF6p) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Infinix X6831) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
    # Firefox / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    # Safari / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Edge / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

_ACCEPT_LANGUAGES = [
    "en-NG,en;q=0.9,yo;q=0.8",
    "en-NG,en-GB;q=0.9,en;q=0.8",
    "en-US,en-NG;q=0.9,en;q=0.8",
    "en-GB,en-NG;q=0.9,en;q=0.8",
    "en-NG,en;q=0.9",
]

_REFERERS = [
    "https://www.google.com.ng/",
    "https://www.google.com/",
    "https://bing.com/",
    "https://t.co/",
    "",   # direct
]


def random_ua() -> str:
    return random.choice(_USER_AGENTS)


def random_headers(referer: str = "") -> dict:
    ua = random_ua()
    is_mobile = "Mobile" in ua or "Android" in ua

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none" if not referer else "cross-site",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    if referer:
        headers["Referer"] = referer
    else:
        ref = random.choice(_REFERERS)
        if ref:
            headers["Referer"] = ref

    if "Chrome" in ua:
        # Add Sec-CH-UA headers for Chrome
        version = "124"
        try:
            version = ua.split("Chrome/")[1].split(".")[0]
        except Exception:
            pass
        headers["Sec-CH-UA"] = f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not-A.Brand";v="99"'
        headers["Sec-CH-UA-Mobile"] = "?1" if is_mobile else "?0"
        headers["Sec-CH-UA-Platform"] = '"Android"' if is_mobile else '"Windows"'

    return headers


def session_headers(site: str = "generic") -> dict:
    """Return headers with site-appropriate referer."""
    site_referers = {
        "jumia":  "https://www.jumia.com.ng/",
        "konga":  "https://www.konga.com/",
        "slot":   "https://www.slot.ng/",
        "temu":   "https://www.temu.com/",
        "jiji":   "https://jiji.ng/",
    }
    return random_headers(referer=site_referers.get(site, ""))
