"""
Scraper registry.

To add a new store:
  1. Create scrapers/<storename>.py subclassing BaseScraper
  2. Add it to REGISTRY below
  3. Add to config.ENABLED_SCRAPERS if wanted by default
  Nothing else changes — the pipeline picks it up automatically.
"""

from .base import BaseScraper, Product
from .jumia import JumiaScraper
from .konga import KongaScraper
from .slot import SlotScraper
from .jiji import JijiScraper
from .temu import TemuScraper

# ── Registry: name → class ─────────────────────────────────
REGISTRY: dict[str, type[BaseScraper]] = {
    "jumia": JumiaScraper,
    "jiji":  JijiScraper,
    "konga": KongaScraper,
    "slot":  SlotScraper,
    # "temu":  TemuScraper,
}


def build_scrapers(
    enabled: list[str] | None = None,
    scraperapi_key: str | None = None,
    use_playwright: bool = True,
) -> dict[str, BaseScraper]:
    """Instantiate and return only enabled scrapers."""
    import config
    enabled = enabled or config.ENABLED_SCRAPERS

    scrapers = {}
    for name in enabled:
        cls = REGISTRY.get(name.lower())
        if cls:
            scrapers[name] = cls(
                scraperapi_key=scraperapi_key or config.SCRAPERAPI_KEY,
                use_playwright=use_playwright,
            )
        else:
            import logging
            logging.getLogger(__name__).warning(f"Unknown scraper: {name}")

    return scrapers


__all__ = [
    "BaseScraper", "Product",
    "JumiaScraper", "KongaScraper", "SlotScraper", "JijiScraper", "TemuScraper",
    "REGISTRY", "build_scrapers",
]
