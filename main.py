"""
Sift — main entry point with Web Service health check emulation.

Usage:
    python main.py           # start the Telegram bot + dummy web server
    python main.py --test    # run a single test search (no bot)
    python main.py --health  # check all scrapers
"""

import sys
import os
import logging
import argparse
import asyncio
from fastapi import FastAPI
import uvicorn

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
logging.getLogger("uvicorn").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ── Render Health Check Server setup ───────────────────────────
app = FastAPI()

@app.get("/")
@app.get("/healthz")
def health_check():
    """Satisfies Render's HTTP health check to keep the service alive."""
    return {"status": "healthy", "service": "Sift Bot Backend"}


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


async def run_bot_and_server():
    """Runs the dummy web app and the Telegram bot concurrently inside the same loop."""
    from bot.telegram_bot import run_bot

    # Get port assigned by Render, default to 8000 locally
    port = int(os.getenv("PORT", 8000))
    
    # Configure uvicorn to run programmatically on the async loop
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    logger.info("Starting concurrent Web Server (Health Check Wrapper) and Telegram Bot...")
    
    # Run the web server and the blocking telegram bot setup together
    # wrapping run_bot in to_thread if it's completely blocking/synchronous
    await asyncio.gather(
        server.serve(),
        asyncio.to_thread(run_bot)
    )


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
        # Default: Run the async initialization wrapper
        asyncio.run(run_bot_and_server())


if __name__ == "__main__":
    main()