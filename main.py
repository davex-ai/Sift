"""
Sift — main entry point.

Usage:
    python main.py           # start the Telegram bot
    python main.py --test    # run a single test search (no bot)
    python main.py --health  # check all scrapers
"""

import sys
import logging
import argparse

# ── Logging setup ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def run_test(query: str = "best blender under 50000") -> None:
    """Quick smoke test — run a search and print results."""
    from pipeline.pipeline import ShoppingPipeline
    from llm.synthesizer import get_synthesizer
    from utils.currency import format_ngn

    print(f"\n{'='*60}")
    print(f"  SIFT TEST SEARCH")
    print(f"  Query: {query}")
    print(f"{'='*60}\n")

    pipeline = ShoppingPipeline()
    groups, intent = pipeline.search(query)

    if not groups:
        print("❌ No results found.")
        return

    print(f"Found {len(groups)} unique product groups\n")

    for i, g in enumerate(groups[:5], 1):
        print(f"{i}. {g.canonical_title}")
        if g.brand:
            print(f"   Brand: {g.brand}")
        if g.specs:
            print(f"   Specs: {g.specs}")
        for s in sorted(g.sources, key=lambda x: x.price_ngn or float("inf")):
            print(f"   • {s.store.capitalize()}: {s.price_display} — {s.url[:60]}")
        if g.avg_rating:
            print(f"   Rating: ⭐ {g.avg_rating} ({g.total_reviews} reviews)")
        print()

    synth = get_synthesizer()
    rec = synth.synthesize(groups, query=query, budget_ngn=intent.budget_ngn)
    print("─" * 60)
    print("AI Recommendation:")
    print(rec)
    print("─" * 60)


def run_health_check() -> None:
    """Check that all scrapers return results."""
    from scrapers import build_scrapers

    scrapers = build_scrapers()
    test_query = "samsung phone"

    print(f"\nHealth check — query: '{test_query}'\n")
    all_ok = True

    for name, scraper in scrapers.items():
        try:
            products = scraper.search(test_query, max_results=3)
            status = f"✅ {len(products)} results"
            if len(products) == 0:
                status = "⚠️  0 results (selector may be stale)"
                all_ok = False
        except Exception as e:
            status = f"❌ Error: {e}"
            all_ok = False

        print(f"  {name:<10} {status}")

    print(f"\n{'All scrapers OK ✅' if all_ok else 'Issues detected ⚠️'}\n")


def main():
    parser = argparse.ArgumentParser(description="Sift — Nigerian Price Comparison Bot")
    parser.add_argument("--test", metavar="QUERY", nargs="?",
                        const="best blender under 50000",
                        help="Run a test search")
    parser.add_argument("--health", action="store_true",
                        help="Check scraper health")
    args = parser.parse_args()

    if args.test is not None:
        run_test(args.test)
    elif args.health:
        run_health_check()
    else:
        # Default: start the Telegram bot
        logger.info("Starting Sift Telegram bot...")
        from bot.telegram_bot import run_bot
        run_bot()


if __name__ == "__main__":
    main()
