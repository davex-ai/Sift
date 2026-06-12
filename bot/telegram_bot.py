"""
bot/telegram_bot.py — updated to register /admin command
"""

import logging
import asyncio
from datetime import datetime

from telegram import BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from bot.handlers import (
    cmd_start, cmd_help, cmd_alerts, cmd_cancel, cmd_admin,
    handle_message, handle_callback, error_handler,
    get_pipeline,
)
from db.models import init_db, session_scope, PriceAlert
from bot.formatter import format_alert_triggered
from utils.currency import format_ngn
import config

logger = logging.getLogger(__name__)


async def run_alert_check(app: Application) -> None:
    logger.info("[Alerts] Starting alert checker")
    while True:
        try:
            await _check_alerts(app)
        except Exception as e:
            logger.error(f"[Alerts] Checker error: {e}")
        await asyncio.sleep(6 * 3600)


async def _check_alerts(app: Application) -> None:
    with session_scope() as db:
        active = db.query(PriceAlert).filter_by(is_active=True).all()
    if not active:
        return

    logger.info(f"[Alerts] Checking {len(active)} active alerts")
    pipeline = get_pipeline()
    checked: dict[str, list] = {}

    for alert in active:
        title = alert.product_title or ""
        if not title:
            continue

        if title not in checked:
            try:
                groups, _ = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: pipeline.search(title, top_n=5)
                )
                checked[title] = groups
            except Exception as e:
                logger.error(f"[Alerts] Search failed for '{title}': {e}")
                checked[title] = []

        groups = checked[title]
        if not groups:
            continue

        matching = next(
            (g for g in groups if g.id == alert.product_id),
            groups[0],
        )
        if not matching.lowest_price:
            continue

        if matching.lowest_price <= alert.threshold_ngn:
            best_source = next(
                (s for s in matching.sources if s.price_ngn == matching.lowest_price),
                matching.sources[0],
            )
            try:
                msg = format_alert_triggered(
                    product_title=matching.canonical_title,
                    current_price=matching.lowest_price,
                    threshold=alert.threshold_ngn,
                    store=best_source.store,
                    url=best_source.url,
                )
                await app.bot.send_message(
                    chat_id=alert.user_id,
                    text=msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                logger.info(f"[Alerts] Triggered for user {alert.user_id}: {matching.canonical_title}")
                with session_scope() as db:
                    a = db.query(PriceAlert).filter_by(id=alert.id).first()
                    if a:
                        a.triggered_at = datetime.utcnow()
                        a.is_active = False
            except Exception as e:
                logger.error(f"[Alerts] Send failed to {alert.user_id}: {e}")


def build_application() -> Application:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set.")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("admin",  cmd_admin))   # ← new
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    return app


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",  "Welcome message"),
        BotCommand("help",   "How to use Sift"),
        BotCommand("alerts", "Your price alerts"),
        BotCommand("cancel", "Cancel current action"),
    ])
    asyncio.create_task(run_alert_check(app))
    logger.info("[Bot] Sift is online ✅")


def run_bot() -> None:
    init_db()
    app = build_application()
    app.post_init = post_init
    # logger.info("[Bot] Starting polling...")
    # app.run_polling(
    #     allowed_updates=["message", "callback_query"],
    #     drop_pending_updates=True,
    #     close_loop=False,         # <--- Add this
    #     stop_signals=None
    # )
    return app