"""
Handlers — all Telegram bot command and message handlers.

Commands: /start  /help  /alerts  /cancel
Messages: free-text search queries, threshold input for alerts
Callbacks: Next/Prev page, Set Alert, Cancel Alert
"""

import logging
import asyncio
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode, ChatAction

from bot.session import get_session, clear_session
from bot.formatter import (
    format_results_page, format_no_results, format_error,
    format_rate_limited, format_help, format_welcome,
    format_alerts_list, format_alert_triggered,
)
from pipeline.pipeline import ShoppingPipeline
from llm.synthesizer import get_synthesizer
from db.models import PriceAlert, init_db, session_scope
from utils.currency import format_ngn
import config

logger = logging.getLogger(__name__)

# Shared pipeline (init once, reuse)
_pipeline: ShoppingPipeline = None


def get_pipeline() -> ShoppingPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = ShoppingPipeline()
    return _pipeline


# ── Callback data constants ───────────────────────────────────
CB_NEXT      = "page:next"
CB_PREV      = "page:prev"
CB_SET_ALERT = "alert:set:{product_id}"
CB_DEL_ALERT = "alert:del:{alert_id}"


# ══════════════════════════════════════════════════════════════
# Command handlers
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        format_welcome(user.first_name or "there"),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        format_help(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with session_scope() as db:
        alerts = (
            db.query(PriceAlert)
            .filter_by(user_id=user_id, is_active=True)
            .all()
        )
        alert_list = list(alerts)

    msg = format_alerts_list(alert_list)
    keyboard = []
    for a in alert_list:
        keyboard.append([
            InlineKeyboardButton(
                f"❌ Cancel: {a.product_title[:30]}",
                callback_data=f"alert:del:{a.id}",
            )
        ])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    sess = get_session(user_id)
    sess.reset_alert_flow()
    await update.message.reply_text(
        "✅ Cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ══════════════════════════════════════════════════════════════
# Message handler — the main search flow
# ══════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()

    if not text:
        return

    sess = get_session(user.id, user.username or "")

    # ── Alert threshold input flow ────────────────────────────
    if sess.awaiting_threshold:
        await _handle_alert_threshold(update, sess)
        return

    # ── Rate limiting ─────────────────────────────────────────
    if sess.is_rate_limited():
        wait = sess.time_until_allowed()
        await update.message.reply_text(format_rate_limited(wait))
        return

    # Validate query length
    if len(text) > config.BOT_MAX_QUERY_LENGTH:
        await update.message.reply_text(
            f"❌ Query too long (max {config.BOT_MAX_QUERY_LENGTH} chars)."
        )
        return

    sess.record_request()
    sess.reset_pagination()
    sess.last_query = text

    # ── Search ───────────────────────────────────────────────
    await update.message.reply_chat_action(ChatAction.TYPING)

    # Searching indicator
    searching_msg = await update.message.reply_text(
        f"🔎 Searching across {len(config.ENABLED_SCRAPERS)} stores for: "
        f"*{text[:60]}*",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        pipeline = get_pipeline()
        groups, intent = await pipeline.search_async(text, top_n=config.TOP_N_RESULTS)

        sess.last_groups = groups
        sess.last_intent = intent

        if not groups:
            await searching_msg.delete()
            await update.message.reply_text(
                format_no_results(text),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # LLM synthesis
        await update.message.reply_chat_action(ChatAction.TYPING)
        synth = get_synthesizer()
        page_groups = sess.current_page_groups()

        llm_text = ""
        try:
            llm_text = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: synth.synthesize(
                    groups[:5],
                    query=text,
                    budget_ngn=intent.budget_ngn,
                ),
            )
        except Exception as e:
            logger.error(f"[Handler] LLM failed: {e}")

        # Format and send
        msg = format_results_page(
            groups=page_groups,
            page=sess.page,
            total_pages=sess.total_pages(),
            query=text,
            budget_ngn=intent.budget_ngn,
            llm_text=llm_text,
        )

        reply_markup = _build_keyboard(sess, groups)

        await searching_msg.delete()
        await update.message.reply_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.exception(f"[Handler] Search error: {e}")
        await searching_msg.delete()
        await update.message.reply_text(format_error(str(e)[:80]))


# ── Alert threshold input ──────────────────────────────────────

async def _handle_alert_threshold(update: Update, sess) -> None:
    text = update.message.text.strip()
    from utils.currency import extract_budget

    threshold = extract_budget(text) or _try_parse_number(text)

    if not threshold:
        await update.message.reply_text(
            "❌ Couldn't parse that price. Try: `50000`, `50k`, or `₦50,000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Save alert
    user_id = update.effective_user.id
    with session_scope() as db:
        alert = PriceAlert(
            user_id=user_id,
            product_id=sess.alert_pending_product_id,
            product_title=sess.alert_pending_title,
            threshold_ngn=threshold,
        )
        db.add(alert)

    sess.reset_alert_flow()
    await update.message.reply_text(
        f"✅ Alert set!\n\n"
        f"*{sess.alert_pending_title or 'Product'}*\n"
        f"I'll notify you when price drops below {format_ngn(threshold)}.\n\n"
        f"Manage alerts with /alerts",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )


def _try_parse_number(s: str):
    """Simple number parser for alert threshold."""
    import re
    s = s.replace(",", "").replace("₦", "").strip()
    m = re.search(r"(\d+\.?\d*)\s*(k|m)?", s, re.I)
    if m:
        n = float(m.group(1))
        mult = (m.group(2) or "").lower()
        if mult == "k":
            n *= 1000
        elif mult == "m":
            n *= 1_000_000
        return n
    return None


# ══════════════════════════════════════════════════════════════
# Callback query handler — buttons
# ══════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    sess = get_session(user.id)
    data = query.data

    # ── Pagination ────────────────────────────────────────────
    if data == CB_NEXT:
        if sess.has_next_page():
            sess.page += 1
            await _update_results_message(query, sess)
        return

    if data == CB_PREV:
        if sess.has_prev_page():
            sess.page -= 1
            await _update_results_message(query, sess)
        return

    # ── Set alert ─────────────────────────────────────────────
    if data.startswith("alert:set:"):
        product_id = data.split("alert:set:")[1]
        group = next((g for g in sess.last_groups if g.id == product_id), None)
        if not group:
            await query.edit_message_text("❌ Product not found.")
            return

        sess.alert_pending_product_id = product_id
        sess.alert_pending_title = group.canonical_title
        sess.awaiting_threshold = True

        await update.effective_message.reply_text(
            f"🔔 *Set Price Alert*\n\n"
            f"*{group.canonical_title}*\n"
            f"Current best price: {format_ngn(group.lowest_price)}\n\n"
            f"What price should I alert you at?\n"
            f"Type an amount, e.g. `450000` or `450k`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Delete alert ──────────────────────────────────────────
    if data.startswith("alert:del:"):
        alert_id = int(data.split("alert:del:")[1])
        with session_scope() as db:
            alert = db.query(PriceAlert).filter_by(id=alert_id).first()
            if alert:
                alert.is_active = False

        await query.edit_message_text("✅ Alert cancelled.")
        return


async def _update_results_message(query, sess) -> None:
    """Re-render the results message for a new page."""
    page_groups = sess.current_page_groups()
    intent = sess.last_intent

    msg = format_results_page(
        groups=page_groups,
        page=sess.page,
        total_pages=sess.total_pages(),
        query=sess.last_query,
        budget_ngn=intent.budget_ngn if intent else None,
        llm_text="",  # don't re-run LLM on page flip
    )
    reply_markup = _build_keyboard(sess, sess.last_groups)

    try:
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"[Handler] Edit message failed: {e}")


# ── Keyboard builder ──────────────────────────────────────────

def _build_keyboard(sess, groups) -> InlineKeyboardMarkup:
    """Build inline keyboard with pagination + alert buttons."""
    rows = []

    # Pagination row
    nav_row = []
    if sess.has_prev_page():
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=CB_PREV))
    if sess.has_next_page():
        nav_row.append(InlineKeyboardButton("Next 5 ➡️", callback_data=CB_NEXT))
    if nav_row:
        rows.append(nav_row)

    # Set alert button for top result on current page
    page_groups = sess.current_page_groups()
    if page_groups:
        top = page_groups[0]
        rows.append([
            InlineKeyboardButton(
                f"🔔 Alert: {top.canonical_title[:25]}",
                callback_data=f"alert:set:{top.id}",
            )
        ])

    return InlineKeyboardMarkup(rows) if rows else None


# ══════════════════════════════════════════════════════════════
# Error handler
# ══════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"[Bot] Unhandled error: {ctx.error}", exc_info=ctx.error)
