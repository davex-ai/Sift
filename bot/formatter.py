"""
Formatter — all Telegram message formatting lives here.

Centralizing formatting makes it easy to:
  - Swap Telegram for WhatsApp later (just change this file)
  - Keep consistent output style
  - Stay within Telegram's 4096 char message limit
"""

from normalizer.dedup import ProductGroup, SourceEntry
from utils.currency import format_ngn
import config

# Store emoji map
STORE_EMOJI = {
    "jumia": "🟠",
    "konga": "🔵",
    "slot":  "🔴",
    "jiji":  "🟢",
    "temu":  "🟣",
}

CONDITION_LABEL = {
    "new":         "🆕 New",
    "used":        "♻️ Used",
    "refurbished": "🔧 Refurbished",
}

MAX_MSG_LEN = 4000  # safe Telegram limit


def format_results_page(
    groups: list[ProductGroup],
    page: int,
    total_pages: int,
    query: str,
    budget_ngn=None,
    llm_text: str = "",
) -> str:
    """
    Format one page of search results as a Telegram message.

    Example output:
    🔍 Results for: best blender under ₦50,000

    1️⃣ Panasonic MX-GM1011
    ⭐ 4.1 · 142 reviews
    💰 Jumia: ₦42,000 → [cheapest]
       Konga: ₦44,500

    2️⃣ Qasa QBL-1850
    ⭐ 4.0
    💰 Jumia: ₦26,600

    🤖 Panasonic offers better build quality...

    Page 1/2 | Next 5 →
    """
    lines = []

    # Header
    budget_str = f" under {format_ngn(budget_ngn)}" if budget_ngn else ""
    lines.append(f"🔍 *Results for:* {_escape(query)}{budget_str}\n")

    if not groups:
        lines.append("❌ No products found. Try different keywords.")
        return "\n".join(lines)

    # Product cards
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
                     "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, g in enumerate(groups):
        emoji = number_emojis[i] if i < len(number_emojis) else f"{i+1}."
        lines.append(_format_product_card(g, emoji))

    # LLM recommendation
    if llm_text:
        lines.append(f"\n🤖 *AI Analysis:*\n{_escape(llm_text)}")

    # Pagination footer
    if total_pages > 1:
        lines.append(f"\n📄 Page {page + 1}/{total_pages}")

    msg = "\n".join(lines)

    # Truncate if over limit
    if len(msg) > MAX_MSG_LEN:
        msg = msg[:MAX_MSG_LEN - 20] + "\n\n_[truncated]_"

    return msg


def _format_product_card(g: ProductGroup, rank_emoji: str) -> str:
    """Format a single ProductGroup as a mini card."""
    lines = []

    # Title + condition
    cond = CONDITION_LABEL.get(g.condition or "", "")
    title_line = f"{rank_emoji} *{_escape(g.canonical_title)}*"
    if cond:
        title_line += f" {cond}"
    lines.append(title_line)

    # Rating + reviews
    if g.avg_rating:
        stars = "⭐" * round(g.avg_rating)
        rating_str = f"⭐ {g.avg_rating}"
        if g.total_reviews:
            rating_str += f" ({g.total_reviews:,} reviews)"
        lines.append(rating_str)

    # Prices per store (sorted cheapest first)
    sorted_sources = sorted(
        g.sources, key=lambda s: s.price_ngn or float("inf")
    )
    price_lines = []
    for j, s in enumerate(sorted_sources):
        store_emoji = STORE_EMOJI.get(s.store, "🏪")
        cheap_tag = " ✅ *cheapest*" if j == 0 and len(sorted_sources) > 1 else ""
        avail_tag = " _(OOS)_" if s.availability == "out_of_stock" else ""
        loc_tag = f" · {s.location}" if s.location else ""
        price_lines.append(
            f"   {store_emoji} {s.store.capitalize()}: "
            f"[{s.price_display}]({s.url}){cheap_tag}{avail_tag}{loc_tag}"
        )
    lines.append("💰 " + price_lines[0].strip())
    lines.extend(price_lines[1:])

    # Price history insight (if available)
    if g.price_30d_avg and g.lowest_price:
        change = g.price_change_pct
        if change and abs(change) >= 3:
            direction = "📉" if change < 0 else "📈"
            lines.append(
                f"   {direction} {abs(change):.0f}% vs 30-day avg "
                f"({format_ngn(g.price_30d_avg)})"
            )

    return "\n".join(lines) + "\n"


def format_no_results(query: str) -> str:
    return (
        f"❌ No results found for *{_escape(query)}*\n\n"
        "Try:\n"
        "• Different keywords\n"
        "• Remove the budget constraint\n"
        "• Check spelling"
    )


def format_error(msg: str = "") -> str:
    return (
        f"⚠️ Something went wrong{': ' + msg if msg else ''}.\n"
        "Please try again in a moment."
    )


def format_rate_limited(seconds: float) -> str:
    return (
        f"⏳ Slow down! You can search again in "
        f"{int(seconds)} seconds."
    )


def format_help() -> str:
    return (
        "🛍️ *Sift — Nigerian Price Comparison Bot*\n\n"
        "Search across Jumia, Konga, Slot, Jiji, and Temu simultaneously.\n\n"
        "*How to search:*\n"
        "Just type what you're looking for:\n"
        "`best blender under ₦50,000`\n"
        "`Samsung Galaxy A15`\n"
        "`cheapest laptop 200k`\n"
        "`iPhone 13 on Jumia`\n\n"
        "*Commands:*\n"
        "/start — Welcome message\n"
        "/help — This message\n"
        "/alerts — View your price alerts\n"
        "/cancel — Cancel current action\n\n"
        "*Price Alerts:*\n"
        "After a search, tap *🔔 Set Alert* to get notified when a "
        "product drops below your target price."
    )


def format_welcome(first_name: str) -> str:
    return (
        f"👋 Welcome, {first_name}!\n\n"
        "I'm *Sift* — I search Jumia, Konga, Slot, Jiji, and Temu "
        "simultaneously to find you the best deals in Nigeria.\n\n"
        "Just type what you're looking for, like:\n"
        "`best blender under ₦50,000`\n\n"
        "Type /help to see all features."
    )


def format_alerts_list(alerts: list) -> str:
    if not alerts:
        return (
            "You have no active price alerts.\n\n"
            "Search for a product and tap 🔔 Set Alert to create one."
        )
    lines = ["🔔 *Your Active Alerts:*\n"]
    for i, a in enumerate(alerts, 1):
        lines.append(
            f"{i}. {_escape(a.product_title)}\n"
            f"   Alert when below: {format_ngn(a.threshold_ngn)}"
            + (f"\n   Store: {a.target_store}" if a.target_store else "")
        )
    return "\n".join(lines)


def format_alert_triggered(
    product_title: str,
    current_price: float,
    threshold: float,
    store: str,
    url: str,
) -> str:
    return (
        f"🔔 *Price Alert!*\n\n"
        f"*{_escape(product_title)}*\n"
        f"Current price: {format_ngn(current_price)} on {store.capitalize()}\n"
        f"Your target:   {format_ngn(threshold)}\n\n"
        f"[Buy now →]({url})"
    )


def _escape(text: str) -> str:
    """Escape Telegram MarkdownV2 special chars in user content."""
    # For parse_mode="Markdown" (v1) — lighter escaping
    if not text:
        return ""
    return (
        text
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )
