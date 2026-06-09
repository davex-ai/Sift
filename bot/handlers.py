"""
bot/handlers.py

Handlers with:
  - Multi-turn clarification flow (category → budget → brand → search)
  - Search animation ("Searching Jumia... ⏳" updates while scraping)
  - 👍/👎 feedback buttons on results
  - Analytics tracking per search
  - Confidence display when intent is uncertain
  - /admin stats command
"""

import logging
import asyncio
import time
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode, ChatAction

from bot.session import (
    get_session, clear_session,
    ConversationSession, get_next_clarification_field, FIELD_QUESTIONS,
)
from bot.formatter import (
    format_results_page, format_no_results, format_error,
    format_rate_limited, format_help, format_welcome,
    format_alerts_list, format_alert_triggered,
)
from pipeline.pipeline import ShoppingPipeline
from pipeline.intent import IntentParser, _is_conversational
from llm.synthesizer import get_synthesizer
from db.models import PriceAlert, init_db, session_scope
from utils.currency import format_ngn, extract_budget, parse_ngn
import config

logger = logging.getLogger(__name__)

# Admin Telegram user IDs (add yours here)
ADMIN_IDS: set[int] = set()

_pipeline: ShoppingPipeline = None

def get_pipeline() -> ShoppingPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = ShoppingPipeline()
    return _pipeline

# ── Callback constants ────────────────────────────────────────
CB_NEXT      = "page:next"
CB_PREV      = "page:prev"
CB_FEEDBACK_GOOD = "fb:good:{search_id}"
CB_FEEDBACK_BAD  = "fb:bad:{search_id}"


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
    await update.message.reply_text(format_help(), parse_mode=ParseMode.MARKDOWN)


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with session_scope() as db:
        alerts = db.query(PriceAlert).filter_by(user_id=user_id, is_active=True).all()
        alert_list = list(alerts)

    msg = format_alerts_list(alert_list)
    keyboard = [
        [InlineKeyboardButton(f"❌ Cancel: {a.product_title[:30]}", callback_data=f"alert:del:{a.id}")]
        for a in alert_list
    ]
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    sess = get_session(user_id)
    sess.reset_alert_flow()
    sess.end_conversation()
    await update.message.reply_text("✅ Cancelled.", reply_markup=ReplyKeyboardRemove())


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin stats command — only visible to ADMIN_IDS."""
    user = update.effective_user
    if user.id not in ADMIN_IDS and ADMIN_IDS:
        return

    try:
        from db.models import get_stats
        stats = get_stats(days=7)
        if not stats:
            await update.message.reply_text("No analytics data yet.")
            return

        lines = [
            "📊 *Sift Stats (last 7 days)*\n",
            f"👥 Total users: {stats['total_users']}",
            f"🟢 Active (7d): {stats['active_users_7d']}",
            f"🔍 Total searches: {stats['total_searches']}",
            f"📈 Searches (7d): {stats.get('searches_7d', 0)}",
            f"📦 Avg results/search: {stats['avg_results']}",
            "\n*Top categories:*",
        ]
        for cat, n in stats["top_categories"]:
            lines.append(f"  • {cat}: {n}")
        lines.append("\n*Top queries:*")
        for q, n in stats["top_queries"]:
            lines.append(f"  • {q}: {n}x")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Stats error: {e}")


# ══════════════════════════════════════════════════════════════
# Search animation helpers
# ══════════════════════════════════════════════════════════════

STORE_EMOJIS = {
    "jumia": "🟠 Jumia",
    "konga": "🔵 Konga",
    "slot":  "🔴 Slot",
    "jiji":  "🟢 Jiji",
    "temu":  "🟣 Temu",
}

async def animate_search(msg, stores: list[str], stop_event: asyncio.Event):
    """
    Cycles through search animation frames while scraping runs.
    Updates the message every ~2 seconds with which stores are still running.
    """
    frames = ["⏳", "🔎", "⌛", "🔍"]
    frame_idx = 0
    pending = list(stores)

    while not stop_event.is_set():
        icon = frames[frame_idx % len(frames)]
        store_lines = "  ".join(STORE_EMOJIS.get(s, s) for s in pending)
        text = f"{icon} *Searching:*\n{store_lines}\n\n_This may take 15-30 seconds..._"
        try:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        frame_idx += 1
        await asyncio.sleep(2.5)


# ══════════════════════════════════════════════════════════════
# Message handler — main entry point
# ══════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return

    sess = get_session(user.id, user.username or "")

    # ── Alert threshold flow ──────────────────────────────────
    if sess.awaiting_threshold:
        await _handle_alert_threshold(update, sess)
        return

    # ── Clarification flow continuation ───────────────────────
    if sess.conversation.is_active():
        await _handle_clarification_response(update, sess, text)
        return

    # ── Rate limiting ─────────────────────────────────────────
    if sess.is_rate_limited():
        await update.message.reply_text(format_rate_limited(sess.time_until_allowed()))
        return

    if len(text) > config.BOT_MAX_QUERY_LENGTH:
        await update.message.reply_text(f"❌ Query too long (max {config.BOT_MAX_QUERY_LENGTH} chars).")
        return

    sess.record_request()
    sess.reset_pagination()
    sess.last_query = text

    # ── Parse intent first to decide: ask or search ───────────
    await update.message.reply_chat_action(ChatAction.TYPING)

    intent_parser = IntentParser()
    intent = intent_parser.parse(text)

    # If we got a category but it warrants clarification AND we're missing key info
    should_clarify = (
        intent.category is not None
        and intent.budget_max_ngn is None  # no budget given
        and not intent.store_filter        # not store-specific
        and len(text.split()) <= 5         # short query — user probably wants help
        and intent.category in ["laptop", "phone", "tablet", "tv", "camera"]
    )

    if should_clarify:
        from bot.session import get_next_clarification_field, FIELD_QUESTIONS
        sess.start_conversation(intent.category, intent.clean_query)
        next_field = get_next_clarification_field(sess.conversation)
        if next_field:
            sess.conversation.awaiting_field = next_field
            sess.conversation.touch()
            question = FIELD_QUESTIONS[next_field]
            await update.message.reply_text(
                f"🛍️ Looking for a *{intent.category}*!\n\n{question}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    # ── Show confidence note if uncertain ─────────────────────
    confidence_note = ""
    if intent.confidence < 0.6 and intent.parsed_by == "llm":
        confidence_note = f"\n_Searching for: *{intent.clean_query}*_"
    elif intent.confidence < 0.6:
        confidence_note = f"\n_Interpreting as: *{intent.clean_query}*_"

    await _run_search(update, sess, intent, confidence_note)


async def _handle_clarification_response(
    update: Update, sess, text: str
) -> None:
    """User replied to a clarification question."""
    conv = sess.conversation
    field = conv.awaiting_field

    # Parse the response based on what we asked
    skip = text.strip().lower() in ("any", "skip", "no preference", "doesn't matter", "don't care", "idc")

    if field == "budget":
        budget = extract_budget(text.lower()) or _try_parse_number(text)
        if budget:
            conv.budget_max = budget
        elif not skip:
            await update.message.reply_text(
                "❓ Couldn't read that as a price. Try: `150k`, `₦200,000`, or type *any* to skip.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    elif field == "brand":
        if not skip:
            conv.brand = text.strip().title()

    elif field == "storage":
        if not skip:
            conv.storage = text.strip().upper()

    elif field == "ram":
        if not skip:
            conv.ram = text.strip().upper()

    conv.turns += 1
    conv.touch()

    # Ask next question or fire search
    from bot.session import get_next_clarification_field, FIELD_QUESTIONS
    next_field = get_next_clarification_field(conv)

    if next_field:
        conv.awaiting_field = next_field
        question = FIELD_QUESTIONS[next_field]
        await update.message.reply_text(question, parse_mode=ParseMode.MARKDOWN)
        return

    # All done — build query and search
    final_query = conv.build_query()
    budget = conv.budget_max

    sess.end_conversation()
    sess.record_request()
    sess.reset_pagination()
    sess.last_query = final_query

    await update.message.reply_text(
        f"🔍 Got it! Searching for *{final_query}*"
        + (f" under {format_ngn(budget)}" if budget else ""),
        parse_mode=ParseMode.MARKDOWN,
    )

    intent_parser = IntentParser()
    intent = intent_parser.parse(
        final_query + (f" under {int(budget)}" if budget else "")
    )
    await _run_search(update, sess, intent, "")


async def _run_search(update: Update, sess, intent, confidence_note: str) -> None:
    """Execute the actual search with animation."""
    await update.message.reply_chat_action(ChatAction.TYPING)

    active_stores = config.ENABLED_SCRAPERS
    store_display = "  ".join(STORE_EMOJIS.get(s, s) for s in active_stores)
    searching_msg = await update.message.reply_text(
        f"⏳ *Searching:*\n{store_display}\n\n_This may take 15-30 seconds..._"
        + confidence_note,
        parse_mode=ParseMode.MARKDOWN,
    )

    # Start animation in background
    stop_animation = asyncio.Event()
    animation_task = asyncio.create_task(
        animate_search(searching_msg, active_stores, stop_animation)
    )

    t0 = time.monotonic()
    try:
        pipeline = get_pipeline()
        groups, intent = await pipeline.search_async(
            intent.raw_query, top_n=config.TOP_N_RESULTS
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Track analytics
        try:
            from db.models import save_search_event, upsert_user
            stores_with_results = list({
                s.store for g in groups for s in g.sources
            })
            upsert_user(update.effective_user.id, update.effective_user.username or "")
            save_search_event(
                user_id=update.effective_user.id,
                raw_query=intent.raw_query,
                clean_query=intent.clean_query,
                category=intent.category,
                result_count=len(groups),
                stores_searched=active_stores,
                stores_with_results=stores_with_results,
                response_time_ms=elapsed_ms,
                parsed_by=intent.parsed_by,
                intent_confidence=intent.confidence,
            )
        except Exception as e:
            logger.debug(f"[Analytics] tracking failed: {e}")

        sess.last_groups = groups
        sess.last_intent = intent

    except Exception as e:
        logger.exception(f"[Handler] Search error: {e}")
        stop_animation.set()
        animation_task.cancel()
        await searching_msg.delete()
        await update.message.reply_text(format_error(str(e)[:80]))
        return

    finally:
        stop_animation.set()
        animation_task.cancel()

    if not groups:
        await searching_msg.delete()
        await update.message.reply_text(
            format_no_results(intent.raw_query),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # LLM synthesis
    await update.message.reply_chat_action(ChatAction.TYPING)
    synth = get_synthesizer()
    llm_text = ""
    try:
        llm_text = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: synth.synthesize(groups[:5], query=intent.raw_query, budget_ngn=intent.budget_ngn),
        )
    except Exception as e:
        logger.error(f"[Handler] LLM failed: {e}")

    page_groups = sess.current_page_groups()
    msg = format_results_page(
        groups=page_groups,
        page=sess.page,
        total_pages=sess.total_pages(),
        query=intent.raw_query,
        budget_ngn=intent.budget_ngn,
        llm_text=llm_text,
    )

    # Build keyboard with feedback buttons
    reply_markup = _build_keyboard(sess, groups, include_feedback=True)

    await searching_msg.delete()
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


# ── Alert threshold ───────────────────────────────────────────

async def _handle_alert_threshold(update: Update, sess) -> None:
    text = update.message.text.strip()
    threshold = extract_budget(text.lower()) or _try_parse_number(text)

    if not threshold:
        await update.message.reply_text(
            "❌ Couldn't parse that price. Try: `50000`, `50k`, or `₦50,000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    user_id = update.effective_user.id
    with session_scope() as db:
        alert = PriceAlert(
            user_id=user_id,
            product_id=sess.alert_pending_product_id,
            product_title=sess.alert_pending_title,
            threshold_ngn=threshold,
        )
        db.add(alert)

    title = sess.alert_pending_title or "Product"
    sess.reset_alert_flow()
    await update.message.reply_text(
        f"✅ *Alert set!*\n\n*{title}*\nI'll notify you when price drops below {format_ngn(threshold)}.\n\n/alerts to manage.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )


def _try_parse_number(s: str):
    import re
    s = s.replace(",", "").replace("₦", "").strip()
    m = re.search(r"(\d+\.?\d*)\s*(k|m)?", s, re.I)
    if m:
        n = float(m.group(1))
        mult = (m.group(2) or "").lower()
        if mult == "k": n *= 1000
        elif mult == "m": n *= 1_000_000
        return n
    return None


# ══════════════════════════════════════════════════════════════
# Callback handler
# ══════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    sess = get_session(user.id)
    data = query.data

    # ── Pagination ────────────────────────────────────────────
    if data == CB_NEXT and sess.has_next_page():
        sess.page += 1
        await _update_results_message(query, sess)
        return

    if data == CB_PREV and sess.has_prev_page():
        sess.page -= 1
        await _update_results_message(query, sess)
        return

    # ── Feedback ─────────────────────────────────────────────
    if data.startswith("fb:good:") or data.startswith("fb:bad:"):
        sentiment = "👍 Thanks for the feedback!" if data.startswith("fb:good:") else "👎 Got it — we'll improve!"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer(sentiment, show_alert=False)
        # TODO: persist feedback to DB
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
            f"🔔 *Set Price Alert*\n\n*{group.canonical_title}*\n"
            f"Current best: {format_ngn(group.lowest_price)}\n\n"
            f"Alert me when price drops below? (e.g. `450000` or `450k`)",
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
    page_groups = sess.current_page_groups()
    intent = sess.last_intent
    msg = format_results_page(
        groups=page_groups,
        page=sess.page,
        total_pages=sess.total_pages(),
        query=sess.last_query,
        budget_ngn=intent.budget_ngn if intent else None,
        llm_text="",
    )
    reply_markup = _build_keyboard(sess, sess.last_groups, include_feedback=False)
    try:
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"[Handler] Edit failed: {e}")


def _build_keyboard(sess, groups, include_feedback: bool = True) -> InlineKeyboardMarkup:
    rows = []

    # Pagination
    nav_row = []
    if sess.has_prev_page():
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=CB_PREV))
    if sess.has_next_page():
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=CB_NEXT))
    if nav_row:
        rows.append(nav_row)

    # Alert for top result
    page_groups = sess.current_page_groups()
    if page_groups:
        top = page_groups[0]
        rows.append([
            InlineKeyboardButton(
                f"🔔 Alert: {top.canonical_title[:22]}",
                callback_data=f"alert:set:{top.id}",
            )
        ])

    # Feedback buttons
    if include_feedback and groups:
        import time as _time
        search_id = str(int(_time.monotonic() * 1000))[-8:]
        rows.append([
            InlineKeyboardButton("👍 Helpful", callback_data=f"fb:good:{search_id}"),
            InlineKeyboardButton("👎 Not helpful", callback_data=f"fb:bad:{search_id}"),
        ])

    return InlineKeyboardMarkup(rows) if rows else None


# ══════════════════════════════════════════════════════════════
# Error handler
# ══════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"[Bot] Unhandled error: {ctx.error}", exc_info=ctx.error)