"""
bot/handlers.py

Full handler with:
  - Intent clarification flow (ask before search when ambiguity >= 0.65)
  - Multi-turn conversation session (category → budget → brand → search)
  - Search animation that updates while scraping runs
  - Confidence note shown when intent was uncertain
  - 👍/👎 feedback buttons
  - Analytics tracking
  - /admin stats command
  - Short welcome guide on /start
"""

import logging
import asyncio
import time
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode, ChatAction

from bot.session import (
    get_session, clear_session,
    get_next_clarification_field, FIELD_QUESTIONS,
)
from bot.formatter import (
    format_results_page, format_no_results, format_error,
    format_rate_limited, format_help, format_welcome,
    format_alerts_list, format_alert_triggered,
)
from pipeline.pipeline import ShoppingPipeline
from pipeline.intent import IntentParser
from llm.synthesizer import get_synthesizer
from db.models import PriceAlert, init_db, session_scope
from utils.currency import format_ngn, extract_budget
import config

logger = logging.getLogger(__name__)

# Add your Telegram user ID here to enable /admin
ADMIN_IDS: set[int] = set()

_pipeline: ShoppingPipeline = None

def get_pipeline() -> ShoppingPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = ShoppingPipeline()
    return _pipeline


STORE_EMOJIS = {
    "jumia": "🟠 Jumia",
    "konga": "🔵 Konga",
    "slot":  "🔴 Slot",
    "jiji":  "🟢 Jiji",
    "temu":  "🟣 Temu",
}

CB_NEXT = "page:next"
CB_PREV = "page:prev"


# ══════════════════════════════════════════════════════════════
# Welcome guide
# ══════════════════════════════════════════════════════════════

WELCOME_GUIDE = """👋 Welcome to *Sift*, {name}\\!

I search Jumia, Konga, Slot, and Jiji *at the same time* to find you the best prices in Nigeria\\.

*How to search:*
Just type what you're looking for — naturally\\.

✅ `Samsung Galaxy A15`
✅ `laptop under 150k`
✅ `iPhone charger`
✅ `I need something to charge my MacBook`
✅ `round thing content creators use for lighting`

*I understand Nigerian shopping context:*
• Say *NEPA took light* → I'll find UPS/generators
• Say *tokunbo* → I'll filter for used items
• Say *under 50k* → budget filter applied automatically

*Commands:*
/help — full feature guide
/alerts — your saved price alerts
/cancel — cancel current action

Just type and I'll handle the rest 🛍️"""


# ══════════════════════════════════════════════════════════════
# Command handlers
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = (user.first_name or "there").replace("_", "\\_").replace("*", "\\*")
    await update.message.reply_text(
        WELCOME_GUIDE.format(name=name),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_help(), parse_mode=ParseMode.MARKDOWN)


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with session_scope() as db:
        alerts = db.query(PriceAlert).filter_by(user_id=user_id, is_active=True).all()
        alert_list = list(alerts)

    keyboard = [
        [InlineKeyboardButton(f"❌ Cancel: {a.product_title[:30]}", callback_data=f"alert:del:{a.id}")]
        for a in alert_list
    ]
    await update.message.reply_text(
        format_alerts_list(alert_list),
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
    user = update.effective_user
    if ADMIN_IDS and user.id not in ADMIN_IDS:
        return
    try:
        from db.models import get_stats
        stats = get_stats(days=7)
        if not stats:
            await update.message.reply_text("No analytics data yet.")
            return
        lines = [
            "📊 *Sift Stats — last 7 days*\n",
            f"👥 Total users: {stats['total_users']}",
            f"🟢 Active (7d): {stats['active_users_7d']}",
            f"🔍 Total searches: {stats['total_searches']}",
            f"📈 Searches (7d): {stats.get('searches_7d', 0)}",
            f"📦 Avg results/search: {stats['avg_results']}",
            f"❌ Zero-result rate: {stats.get('zero_result_pct', 0)}%",
            "\n*Top categories:*",
        ]
        for cat, n in stats.get("top_categories", []):
            lines.append(f"  • {cat}: {n}")
        lines.append("\n*Top queries:*")
        for q, n in stats.get("top_queries", []):
            lines.append(f"  • {q}: {n}x")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Stats error: {e}")


# ══════════════════════════════════════════════════════════════
# Search animation
# ══════════════════════════════════════════════════════════════

async def _animate_search(msg, stores: list, stop_event: asyncio.Event):
    """Cycles animation frames on the 'Searching...' message while scraping runs."""
    frames   = ["⏳", "🔎", "⌛", "🔍"]
    idx      = 0
    store_line = "  ".join(STORE_EMOJIS.get(s, s) for s in stores)

    while not stop_event.is_set():
        icon = frames[idx % len(frames)]
        try:
            await msg.edit_text(
                f"{icon} *Searching:*\n{store_line}\n\n_This takes 15-30 seconds..._",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        idx += 1
        await asyncio.sleep(2.5)


# ══════════════════════════════════════════════════════════════
# Main message handler
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

    # ── Multi-turn conversation continuation ──────────────────
    if sess.conversation.is_active():
        await _handle_conversation_turn(update, sess, text)
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

    await update.message.reply_chat_action(ChatAction.TYPING)

    # ── Parse intent ──────────────────────────────────────────
    intent_parser = IntentParser()
    intent = intent_parser.parse(text)

    # ── LLM says ask a question (high ambiguity) ──────────────
    if intent.needs_clarification and not intent.should_search():
        # Store partial intent in conversation session for context
        sess.start_conversation(
            category=intent.category or "",
            base_query=intent.canonical_search_term,
        )
        if intent.budget_max_ngn:
            sess.conversation.budget_max = intent.budget_max_ngn
        if intent.brand:
            sess.conversation.brand = intent.brand

        # Show confidence note so user knows what we understood
        conf_note = ""
        if intent.canonical_search_term and intent.canonical_search_term.lower() != text.lower():
            conf_note = f"\n_I think you're looking for: *{intent.canonical_search_term}*_\n"

        await update.message.reply_text(
            f"🤔{conf_note}\n{intent.clarification_question}",
            parse_mode=ParseMode.MARKDOWN,
        )
        sess.conversation.awaiting_field = "free_text_answer"
        sess.conversation.touch()
        return

    # ── Multi-turn clarification triggered by category + no budget ─
    should_clarify = (
        intent.category is not None
        and intent.budget_max_ngn is None
        and not intent.store_filter
        and len(text.split()) <= 4
        and intent.category in ["laptop", "phone", "tablet", "tv", "camera"]
    )

    if should_clarify:
        sess.start_conversation(intent.category, intent.canonical_search_term)
        next_field = get_next_clarification_field(sess.conversation)
        if next_field:
            sess.conversation.awaiting_field = next_field
            sess.conversation.touch()
            await update.message.reply_text(
                f"🛍️ Looking for a *{intent.category}*!\n\n{FIELD_QUESTIONS[next_field]}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    await _run_search(update, sess, intent)


# ══════════════════════════════════════════════════════════════
# Conversation turn handler
# ══════════════════════════════════════════════════════════════

async def _handle_conversation_turn(update: Update, sess, text: str) -> None:
    conv  = sess.conversation
    field = conv.awaiting_field
    skip  = text.strip().lower() in ("any", "skip", "no preference", "doesn't matter", "idc", "no")

    if field == "free_text_answer":
        # User answered the LLM's clarification question
        # Build enriched query and search
        enriched = f"{conv.base_query} {text}".strip()
        sess.end_conversation()
        sess.record_request()
        sess.reset_pagination()
        sess.last_query = enriched

        intent_parser = IntentParser()
        intent = intent_parser.parse(enriched)
        await _run_search(update, sess, intent)
        return

    elif field == "budget":
        budget = extract_budget(text.lower()) or _try_parse_number(text)
        if budget:
            conv.budget_max = budget
        elif not skip:
            await update.message.reply_text(
                "❓ Couldn't read that price. Try `150k`, `₦200,000`, or type *any* to skip.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    elif field == "brand":
        if not skip:
            conv.brand = text.strip().title()

    conv.turns += 1
    conv.touch()

    next_field = get_next_clarification_field(conv)
    if next_field:
        conv.awaiting_field = next_field
        await update.message.reply_text(FIELD_QUESTIONS[next_field], parse_mode=ParseMode.MARKDOWN)
        return

    # All fields collected — build final query and search
    final_query = conv.build_query()
    budget      = conv.budget_max
    sess.end_conversation()
    sess.record_request()
    sess.reset_pagination()
    sess.last_query = final_query

    confirm = f"🔍 Searching for *{final_query}*"
    if budget:
        confirm += f" under {format_ngn(budget)}"
    await update.message.reply_text(confirm, parse_mode=ParseMode.MARKDOWN)

    intent_parser = IntentParser()
    suffix = f" under {int(budget)}" if budget else ""
    intent = intent_parser.parse(final_query + suffix)
    await _run_search(update, sess, intent)


# ══════════════════════════════════════════════════════════════
# Core search runner
# ══════════════════════════════════════════════════════════════

async def _run_search(update: Update, sess, intent) -> None:
    active_stores = config.ENABLED_SCRAPERS
    store_line    = "  ".join(STORE_EMOJIS.get(s, s) for s in active_stores)

    # Build confidence note
    conf_note = ""
    if (
        intent.parsed_by == "llm"
        and intent.canonical_search_term.lower() != intent.raw_query.lower()
        and intent.confidence < 0.85
    ):
        conf_note = f"\n_Searching for: *{intent.canonical_search_term}*_"

    searching_msg = await update.message.reply_text(
        f"⏳ *Searching:*\n{store_line}\n\n_This takes 15-30 seconds..._" + conf_note,
        parse_mode=ParseMode.MARKDOWN,
    )

    stop_anim  = asyncio.Event()
    anim_task  = asyncio.create_task(_animate_search(searching_msg, active_stores, stop_anim))

    t0 = time.monotonic()
    groups = []
    try:
        pipeline = get_pipeline()
        groups, intent = await pipeline.search_async(intent.raw_query, top_n=config.TOP_N_RESULTS)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Analytics
        try:
            from db.models import save_search_event, upsert_user
            stores_hit = list({s.store for g in groups for s in g.sources})
            upsert_user(update.effective_user.id, update.effective_user.username or "")
            save_search_event(
                user_id=update.effective_user.id,
                raw_query=intent.raw_query,
                clean_query=intent.canonical_search_term,
                category=intent.category,
                result_count=len(groups),
                stores_searched=active_stores,
                stores_with_results=stores_hit,
                response_time_ms=elapsed_ms,
                parsed_by=intent.parsed_by,
                intent_confidence=intent.confidence,
            )
        except Exception as e:
            logger.debug(f"[Analytics] {e}")

        sess.last_groups = groups
        sess.last_intent  = intent

    except Exception as e:
        logger.exception(f"[Handler] Search error: {e}")
        stop_anim.set()
        anim_task.cancel()
        await searching_msg.delete()
        await update.message.reply_text(format_error(str(e)[:80]))
        return
    finally:
        stop_anim.set()
        anim_task.cancel()

    await searching_msg.delete()

    # Pipeline returned empty + needs_clarification → the intent layer wants to ask
    if not groups and intent.needs_clarification and intent.clarification_question:
        await update.message.reply_text(
            f"🤔 {intent.clarification_question}",
            parse_mode=ParseMode.MARKDOWN,
        )
        sess.start_conversation(intent.category or "", intent.canonical_search_term)
        sess.conversation.awaiting_field = "free_text_answer"
        sess.conversation.touch()
        return

    if not groups:
        await update.message.reply_text(
            format_no_results(intent.raw_query),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # LLM synthesis
    await update.message.reply_chat_action(ChatAction.TYPING)
    synth    = get_synthesizer()
    llm_text = ""
    try:
        llm_text = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: synth.synthesize(groups[:5], query=intent.raw_query, budget_ngn=intent.budget_ngn),
        )
    except Exception as e:
        logger.error(f"[Handler] LLM failed: {e}")

    msg = format_results_page(
        groups=sess.current_page_groups(),
        page=sess.page,
        total_pages=sess.total_pages(),
        query=intent.raw_query,
        budget_ngn=intent.budget_ngn,
        llm_text=llm_text,
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_build_keyboard(sess, groups, feedback=True),
        disable_web_page_preview=True,
    )


# ── Alert threshold ───────────────────────────────────────────

async def _handle_alert_threshold(update: Update, sess) -> None:
    text      = update.message.text.strip()
    threshold = extract_budget(text.lower()) or _try_parse_number(text)

    if not threshold:
        await update.message.reply_text(
            "❌ Couldn't parse that price. Try `50000`, `50k`, or `₦50,000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    user_id = update.effective_user.id
    with session_scope() as db:
        db.add(PriceAlert(
            user_id=user_id,
            product_id=sess.alert_pending_product_id,
            product_title=sess.alert_pending_title,
            threshold_ngn=threshold,
        ))

    title = sess.alert_pending_title or "Product"
    sess.reset_alert_flow()
    await update.message.reply_text(
        f"✅ *Alert set!*\n\n*{title}*\n"
        f"I'll notify you when price drops below {format_ngn(threshold)}.\n\n"
        "/alerts to manage.",
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
        if mult == "k":   n *= 1000
        elif mult == "m": n *= 1_000_000
        return n
    return None


# ══════════════════════════════════════════════════════════════
# Callback handler
# ══════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user  = update.effective_user
    sess  = get_session(user.id)
    data  = query.data

    if data == CB_NEXT and sess.has_next_page():
        sess.page += 1
        await _refresh_results(query, sess)
        return

    if data == CB_PREV and sess.has_prev_page():
        sess.page -= 1
        await _refresh_results(query, sess)
        return

    if data.startswith("fb:"):
        sentiment = "good" if data.startswith("fb:good:") else "bad"
        reply     = "👍 Thanks!" if sentiment == "good" else "👎 Noted — we'll improve!"
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.answer(reply, show_alert=False)
        return

    if data.startswith("alert:set:"):
        product_id = data.split("alert:set:")[1]
        group = next((g for g in sess.last_groups if g.id == product_id), None)
        if not group:
            await query.edit_message_text("❌ Product not found.")
            return
        sess.alert_pending_product_id = product_id
        sess.alert_pending_title      = group.canonical_title
        sess.awaiting_threshold       = True
        await update.effective_message.reply_text(
            f"🔔 *Set Price Alert*\n\n*{group.canonical_title}*\n"
            f"Current best: {format_ngn(group.lowest_price)}\n\n"
            "Alert me when price drops below? (e.g. `450000` or `450k`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data.startswith("alert:del:"):
        alert_id = int(data.split("alert:del:")[1])
        with session_scope() as db:
            a = db.query(PriceAlert).filter_by(id=alert_id).first()
            if a:
                a.is_active = False
        await query.edit_message_text("✅ Alert cancelled.")
        return


async def _refresh_results(query, sess) -> None:
    intent = sess.last_intent
    msg = format_results_page(
        groups=sess.current_page_groups(),
        page=sess.page,
        total_pages=sess.total_pages(),
        query=sess.last_query,
        budget_ngn=intent.budget_ngn if intent else None,
        llm_text="",
    )
    try:
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_build_keyboard(sess, sess.last_groups, feedback=False),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"[Handler] Edit failed: {e}")


def _build_keyboard(sess, groups, feedback: bool = True) -> InlineKeyboardMarkup:
    rows = []

    nav = []
    if sess.has_prev_page(): nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=CB_PREV))
    if sess.has_next_page(): nav.append(InlineKeyboardButton("Next ➡️",  callback_data=CB_NEXT))
    if nav:
        rows.append(nav)

    page_groups = sess.current_page_groups()
    if page_groups:
        top = page_groups[0]
        rows.append([InlineKeyboardButton(
            f"🔔 Alert: {top.canonical_title[:22]}",
            callback_data=f"alert:set:{top.id}",
        )])

    if feedback and groups:
        sid = str(int(time.monotonic() * 1000))[-8:]
        rows.append([
            InlineKeyboardButton("👍 Helpful",     callback_data=f"fb:good:{sid}"),
            InlineKeyboardButton("👎 Not helpful", callback_data=f"fb:bad:{sid}"),
        ])

    return InlineKeyboardMarkup(rows) if rows else None


# ══════════════════════════════════════════════════════════════
# Error handler
# ══════════════════════════════════════════════════════════════

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"[Bot] Unhandled error: {ctx.error}", exc_info=ctx.error)