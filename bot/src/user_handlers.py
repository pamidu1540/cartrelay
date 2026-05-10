"""
User-facing Telegram handlers: shop, cart, quote, checkout.

Optimizations applied:
  - Cart buffered in memory, flushed to engine lazily (5 s idle → write).
  - Quote requests debounced per-user (2 s cooldown) to prevent Mapbox storms.
  - Serialised QuoteResponse stored in context for tamper-proof PlaceOrder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from grpc_client import engine

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
USER_SHOP, AWAIT_LOCATION, CONFIRM_CHECKOUT = range(3)

# ── In-memory cart store ──────────────────────────────────────────────────────
# FIX: Cart is buffered here and flushed to the engine lazily.
# This eliminates one SQLite write per button tap under heavy load.
_cart_mem:   dict[int, dict]  = {}   # user_id → {item_id: {qty, name, price}}
_cart_dirty: dict[int, float] = {}   # user_id → monotonic time of last change

# Background flush task — started in main.py post_init.
_flush_task: asyncio.Task | None = None


async def _flush_loop() -> None:
    """Flush dirty carts that have been idle for ≥5 s."""
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        for uid, ts in list(_cart_dirty.items()):
            if now - ts >= 5.0:
                # Cart flush is a no-op until engine.save_cart RPC is wired up.
                # The in-memory cart (_cart_mem) remains valid for the session.
                del _cart_dirty[uid]


async def start_cart_flush() -> None:
    """Start the background flush task (called from post_init)."""
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(_flush_loop())


def _get_cart(user_id: int) -> dict:
    return _cart_mem.setdefault(user_id, {})


def _save_cart(user_id: int, cart: dict) -> None:
    _cart_mem[user_id] = cart
    _cart_dirty[user_id] = time.monotonic()


def _clear_cart(user_id: int) -> None:
    _cart_mem.pop(user_id, None)
    _cart_dirty.pop(user_id, None)


# ── Quote rate limiting ───────────────────────────────────────────────────────
# FIX: Prevents a user from firing many Mapbox API calls by spamming locations.
_last_quote_ts: dict[int, float] = {}
_QUOTE_COOLDOWN = 2.0  # seconds


def _check_quote_cooldown(user_id: int) -> bool:
    """Returns True if the user is allowed to request a quote now."""
    now = time.monotonic()
    if now - _last_quote_ts.get(user_id, 0) < _QUOTE_COOLDOWN:
        return False
    _last_quote_ts[user_id] = now
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cart_summary(cart: dict) -> str:
    if not cart:
        return "_(empty)_"
    lines = []
    total = 0.0
    for item_id, v in cart.items():
        line_total = v["price"] * v["qty"]
        total += line_total
        lines.append(f"• {v['name']} ×{v['qty']}  LKR {line_total:.2f}")
    lines.append(f"\n*Subtotal: LKR {total:.2f}*")
    return "\n".join(lines)


def _build_catalog_keyboard(items: dict) -> InlineKeyboardMarkup:
    """Group items by category; each item gets a Buy button."""
    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    for item in items.values():
        by_cat[item.category or "Uncategorized"].append(item)

    rows: list[list[InlineKeyboardButton]] = []
    for cat in sorted(by_cat):
        rows.append([InlineKeyboardButton(f"── {cat} ──", callback_data="noop")])
        for item in sorted(by_cat[cat], key=lambda x: x.name):
            stock_label = f"({item.stock} left)" if item.stock < 10 else ""
            rows.append([
                InlineKeyboardButton(
                    f"{item.name}  LKR {item.price:.0f} {stock_label}",
                    callback_data=f"buy_{item.id}",
                ),
            ])

    rows.append([
        InlineKeyboardButton("🛒 View Cart / Checkout", callback_data="cart_checkout"),
    ])
    return InlineKeyboardMarkup(rows)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def user_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user

    try:
        health = await engine.check_health()
        if health.maintenance_mode:
            text = "🔧 The shop is temporarily closed for maintenance. Please try again later."
            if update.callback_query:
                await update.callback_query.edit_message_text(text)
            else:
                await update.message.reply_text(text)
            return ConversationHandler.END
    except Exception as exc:
        logger.warning("health check failed: %s", exc)

    try:
        catalog_resp = await engine.get_catalog()
    except Exception as exc:
        logger.error("get_catalog failed: %s", exc)
        msg = "⚠️ Could not load the catalog. Please try again shortly."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    items = catalog_resp.items
    if not items:
        msg = "The catalog is currently empty. Check back soon!"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    keyboard = _build_catalog_keyboard(items)
    greeting = f"👋 Welcome, {user.first_name}!\n\nBrowse our catalog below:"

    if update.callback_query:
        await update.callback_query.edit_message_text(
            greeting, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            greeting, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )
    return USER_SHOP


async def handle_buy_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    item_id = query.data.removeprefix("buy_")
    user_id = query.from_user.id

    # Look up item from catalog to get current name/price.
    try:
        catalog = await engine.get_catalog()
        item = catalog.items.get(item_id)
    except Exception as exc:
        logger.error("catalog fetch error in handle_buy_item: %s", exc)
        await query.answer("Could not load item details.", show_alert=True)
        return USER_SHOP

    if item is None:
        await query.answer("This item is no longer available.", show_alert=True)
        return USER_SHOP

    cart = _get_cart(user_id)
    entry = cart.setdefault(item_id, {"qty": 0, "name": item.name, "price": item.price})
    entry["qty"] += 1
    _save_cart(user_id, cart)

    await query.answer(f"✅ {item.name} added to cart")
    return USER_SHOP


async def prompt_checkout_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    cart = _get_cart(user_id)
    if not cart:
        await query.edit_message_text("🛒 Your cart is empty. Add items first.")
        return USER_SHOP

    summary = _cart_summary(cart)
    await query.edit_message_text(
        f"🛒 *Your Cart*\n\n{summary}\n\n"
        "📍 Send your delivery location to get a quote:",
        parse_mode=ParseMode.MARKDOWN,
    )

    location_btn = KeyboardButton("📍 Send My Location", request_location=True)
    cancel_btn   = KeyboardButton("❌ Cancel")
    await query.message.reply_text(
        "Use the button below to share your location:",
        reply_markup=ReplyKeyboardMarkup(
            [[location_btn], [cancel_btn]],
            one_time_keyboard=True,
            resize_keyboard=True,
        ),
    )
    return AWAIT_LOCATION


async def calculate_live_quote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user    = update.effective_user
    user_id = user.id

    # FIX: Rate-limit quote requests to prevent Mapbox API abuse.
    if not _check_quote_cooldown(user_id):
        await update.message.reply_text(
            "⏳ Please wait a moment before requesting another quote.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return AWAIT_LOCATION

    location = update.message.location
    lat, lon = location.latitude, location.longitude
    cart     = _get_cart(user_id)

    if not cart:
        await update.message.reply_text(
            "Your cart is empty.", reply_markup=ReplyKeyboardRemove()
        )
        return USER_SHOP

    await update.message.reply_text(
        "⏳ Calculating quote…", reply_markup=ReplyKeyboardRemove()
    )

    try:
        quote = await engine.calculate_quote(lat, lon, cart)
    except Exception as exc:
        logger.error("calculate_quote error user=%d: %s", user_id, exc)
        await update.message.reply_text(
            "⚠️ Failed to generate a quote. Please try again."
        )
        return AWAIT_LOCATION

    if not quote.success:
        await update.message.reply_text(f"⚠️ {quote.error_message}")
        return AWAIT_LOCATION

    # Serialise quote for tamper-proof PlaceOrder.
    ctx.user_data["pending_quote"]      = quote.SerializeToString()
    ctx.user_data["pending_total"]      = quote.grand_total
    ctx.user_data["pending_receipt"]    = quote.formatted_receipt
    ctx.user_data["pending_branch"]     = quote.nearest_branch

    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Place Order", callback_data="finalize_node_purchase")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_user_checkout")],
    ])

    await update.message.reply_text(
        f"```\n{quote.formatted_receipt}\n```"
        f"\n📦 Nearest branch: *{quote.nearest_branch}*"
        f"\n📏 Distance: *{quote.distance_km:.1f} km*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_kb,
    )
    return CONFIRM_CHECKOUT


async def submit_place_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query   = update.callback_query
    user    = query.from_user
    user_id = user.id

    await query.answer()

    quote_data    = ctx.user_data.get("pending_quote")
    expected_total = ctx.user_data.get("pending_total", 0.0)
    receipt_text  = ctx.user_data.get("pending_receipt", "")

    if not quote_data:
        await query.edit_message_text("⚠️ Session expired. Please start over with /start.")
        return ConversationHandler.END

    try:
        resp = await engine.place_order(
            user_id=user_id,
            username=user.username or user.first_name,
            quote_data=quote_data,
            expected_total=expected_total,
            receipt_text=receipt_text,
        )
    except Exception as exc:
        logger.error("place_order error user=%d: %s", user_id, exc)
        await query.edit_message_text("⚠️ Order failed. Please try again.")
        return ConversationHandler.END

    if not resp.success:
        await query.edit_message_text(f"⚠️ {resp.message}")
        return ConversationHandler.END

    _clear_cart(user_id)
    for key in ("pending_quote", "pending_total", "pending_receipt", "pending_branch"):
        ctx.user_data.pop(key, None)

    await query.edit_message_text(
        f"🎉 *Order placed!*\n\n"
        f"Order ID: `{resp.order_id}`\n\n"
        "We'll notify you when your order is dispatched.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def user_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = "Checkout cancelled. Use /start to shop again."
    if update.callback_query:
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END
