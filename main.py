"""
Sift — main entry point.

Render deployment uses WEBHOOK mode (no polling) to avoid
telegram.error.Conflict when the platform cycles instances.

Set WEBHOOK_URL env var to your Render service URL, e.g.:
    WEBHOOK_URL=https://sift-amcs.onrender.com

Usage:
    python main.py           # start bot + web server (webhook on Render)
    python main.py --test    # run a single test search (no bot)
    python main.py --health  # check all scrapers
"""

import sys
import os
import logging
import argparse
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update
import uvicorn

# ── Logging setup ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ── Global bot application (set during startup) ────────────────
_bot_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: start bot on startup, clean up on shutdown."""
    global _bot_app

    webhook_url = os.getenv("WEBHOOK_URL", "").rstrip("/")

    if webhook_url:
        # ── WEBHOOK MODE (Render production) ──────────────────
        from bot.telegram_bot import run_bot

        _bot_app = run_bot()

        async with _bot_app:
            await _bot_app.start()

            # Register the webhook with Telegram
            hook = f"{webhook_url}/webhook"
            await _bot_app.bot.set_webhook(
                url=hook,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            logger.info(f"[Bot] Webhook registered: {hook}")

            yield  # server is running

            # ── Shutdown ──────────────────────────────────────
            logger.info("[Bot] Removing webhook and stopping...")
            await _bot_app.bot.delete_webhook(drop_pending_updates=True)
            await _bot_app.stop()
    else:
        # ── POLLING MODE (local development fallback) ─────────
        logger.warning("[Bot] WEBHOOK_URL not set — falling back to polling (local dev only)")
        from bot.telegram_bot import run_bot

        _bot_app = run_bot()

        async with _bot_app:
            await _bot_app.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            await _bot_app.start()
            logger.info("[Bot] Polling started (local dev mode)")

            yield  # server is running

            logger.info("[Bot] Stopping polling...")
            await _bot_app.updater.stop()
            await _bot_app.stop()


# ── FastAPI app ────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/healthz")
def health_check():
    """Render HTTP health check."""
    return {"status": "healthy", "service": "Sift Bot Backend"}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook."""
    if _bot_app is None:
        return Response(status_code=503, content="Bot not initialised")

    data = await request.json()
    update = Update.de_json(data, _bot_app.bot)
    await _bot_app.process_update(update)
    return Response(status_code=200)


# ── CLI helpers ────────────────────────────────────────────────
def run_test(query: str = "best blender under 50000") -> None:
    from pipeline.pipeline import ShoppingPipeline
    from llm.synthesizer import get_synthesizer

    print(f"\n{'='*60}\n  SIFT TEST SEARCH\n  Query: {query}\n{'='*60}\n")
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
        for s in sorted(g.sources, key=lambda x: x.price_ngn or float("inf")):
            print(f"   • {s.store.capitalize()}: {s.price_display} — {s.url[:60]}")
        print()

    synth = get_synthesizer()
    rec = synth.synthesize(groups, query=query, budget_ngn=intent.budget_ngn)
    print("─" * 60 + "\nAI Recommendation:\n" + rec + "\n" + "─" * 60)


def run_health_check() -> None:
    from scrapers import build_scrapers

    scrapers = build_scrapers()
    test_query = "samsung phone"
    print(f"\nHealth check — query: '{test_query}'\n")
    all_ok = True

    for name, scraper in scrapers.items():
        try:
            products = scraper.search(test_query, max_results=3)
            status = f"✅ {len(products)} results" if products else "⚠️  0 results (selector may be stale)"
            if not products:
                all_ok = False
        except Exception as e:
            status = f"❌ Error: {e}"
            all_ok = False
        print(f"  {name:<10} {status}")

    print(f"\n{'All scrapers OK ✅' if all_ok else 'Issues detected ⚠️'}\n")


def main():
    parser = argparse.ArgumentParser(description="Sift — Nigerian Price Comparison Bot")
    parser.add_argument("--test", metavar="QUERY", nargs="?",
                        const="best blender under 50000", help="Run a test search")
    parser.add_argument("--health", action="store_true", help="Check scraper health")
    args = parser.parse_args()

    if args.test is not None:
        run_test(args.test)
    elif args.health:
        run_health_check()
    else:
        port = int(os.getenv("PORT", 8000))
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
