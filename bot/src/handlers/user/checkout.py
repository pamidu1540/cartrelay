"""
checkout.py — Checkout flow, address selection, promo codes, delivery time-slots, and order confirmation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

import grpc
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
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from handlers.user.common import (
    STATE_ADDRESS,
    STATE_CONFIRM,
    STATE_PROMO,
    STATE_PROMO_CODE,
    STATE_SLOT,
    _cart,
    _safe_reply,
)

logger = logging.getLogger(__name__)


# ── Checkout Flow Handlers ───────────────────────────────────────────────────

async def cb_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start checkout: ask for delivery address or show saved addresses."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    items = await _cart(context).get_cart(user_id)
    if not items:
        await send_or_edit_main_menu(update, context, "🛒 Your cart is empty. Use /catalog to add items.")
        return ConversationHandler.END

    try:
        addr_resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        saved = addr_resp.addresses
    except Exception:
        saved = []

    kb = []
    for a in saved[:5]:
        label = "⭐ " + a.label if a.is_default else a.label
        kb.append([InlineKeyboardButton(
            f"{label}: {a.address[:40]}",
            callback_data=f"addr_select:{a.id}",
        )])
    kb.append([InlineKeyboardButton("📍 Enter new address", callback_data="addr_select:new")])

    await query.edit_message_text(
        "📍 *Where should we deliver?*\n\nChoose a saved address or enter a new one:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_ADDRESS


async def cb_address_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    addr_id = query.data.split(":", 1)[1]

    if addr_id == "new":
        try:
            await query.message.delete()
        except Exception:
            pass

        reply_kb = [
            [KeyboardButton("📍 Share Location", request_location=True)],
        ]
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📍 Please tap *Share Location* below to share your GPS location, or type your delivery address manually:",
            reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return STATE_ADDRESS

    user_id = update.effective_user.id
    try:
        addr_resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        addr_map = {str(a.id): a for a in addr_resp.addresses}
        addr = addr_map.get(addr_id)
        if addr:
            context.user_data["delivery_address"] = addr.address
            context.user_data["delivery_lat"] = addr.lat
            context.user_data["delivery_lon"] = addr.lon
    except Exception:
        pass

    return await _proceed_to_quote(update, context)


async def msg_delivery_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    msg = update.effective_message
    user_id = update.effective_user.id

    try:
        await msg.delete()
    except Exception:
        pass

    lat = float(os.environ.get("DEFAULT_LAT", "6.9271"))
    lon = float(os.environ.get("DEFAULT_LON", "79.8612"))

    if msg.location:
        lat = msg.location.latitude
        lon = msg.location.longitude
        addr_text = f"GPS: ({lat:.6f}, {lon:.6f})"

        try:
            await pool.call(
                "/logistics.LogisticsEngine/SaveAddress",
                lambda stub, meta: stub.SaveAddress(
                    pb.SavedAddress(
                        user_id=user_id,
                        label="Home",
                        address=addr_text,
                        lat=lat,
                        lon=lon,
                        is_default=True,
                    ),
                    metadata=meta,
                ),
                user_id=user_id,
            )
        except Exception:
            pass
    else:
        addr_text = msg.text.strip()

    context.user_data["delivery_address"] = addr_text
    context.user_data["delivery_lat"] = lat
    context.user_data["delivery_lon"] = lon

    try:
        rm_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📍 Location recorded.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=rm_msg.message_id)
    except Exception:
        pass

    return await _proceed_to_quote(update, context)


async def _proceed_to_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Calculate quote and show promo / loyalty options."""
    user_id = update.effective_user.id
    items = await _cart(context).get_cart(user_id)

    try:
        quote = await pool.call(
            "/logistics.LogisticsEngine/CalculateQuote",
            lambda stub, meta: stub.CalculateQuote(
                pb.QuoteRequest(
                    user_lat=context.user_data.get("delivery_lat", 0),
                    user_lon=context.user_data.get("delivery_lon", 0),
                    cart_items=items,
                    user_id=user_id,
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ Could not calculate quote: {e.details()}")
        return ConversationHandler.END

    context.user_data["quote"] = quote

    kb = [
        [InlineKeyboardButton("🏷 Enter Promo Code", callback_data="promo:enter")],
        [InlineKeyboardButton("⏩ Skip Promo",       callback_data="promo:skip")],
    ]

    try:
        bal = await pool.call(
            "/logistics.LogisticsEngine/GetLoyaltyBalance",
            lambda stub, meta: stub.GetLoyaltyBalance(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        if bal.points > 0:
            kb.insert(0, [InlineKeyboardButton(
                f"🎁 Use {bal.points} loyalty points",
                callback_data=f"promo:loyalty:{bal.points}",
            )])
    except Exception:
        pass

    text = (
        quote.formatted_receipt
        + f"\n\n📍 Delivering to: {context.user_data.get('delivery_address', 'N/A')}"
        + f"\n🕐 Est. delivery: ~{int(quote.distance_km * 3 + 15)} min"
    )
    await send_or_edit_main_menu(update, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return STATE_PROMO


async def cb_use_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "promo:enter":
        await send_or_edit_main_menu(update, context, "🏷 Enter your promo code:")
        return STATE_PROMO_CODE

    if query.data.startswith("promo:loyalty:"):
        pts = int(query.data.split(":")[-1])
        context.user_data["loyalty_redeem"] = pts
        await query.answer(f"✅ {pts} loyalty points will be applied at checkout")

    return await _show_time_slots(update, context)


async def msg_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    code = update.effective_message.text.strip().upper()
    user_id = update.effective_user.id
    items = await _cart(context).get_cart(user_id)

    try:
        quote = await pool.call(
            "/logistics.LogisticsEngine/CalculateQuote",
            lambda stub, meta: stub.CalculateQuote(
                pb.QuoteRequest(
                    user_lat=context.user_data.get("delivery_lat", 0),
                    user_lon=context.user_data.get("delivery_lon", 0),
                    cart_items=items,
                    user_id=user_id,
                    promo_code=code,
                    loyalty_redeem=context.user_data.get("loyalty_redeem", 0),
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )
        if quote.promo_applied:
            context.user_data["quote"] = quote
            context.user_data["promo_code"] = code
            await _safe_reply(update, context,
                f"✅ Promo *{code}* applied! You save LKR {quote.promo_discount:.0f}")
        else:
            await send_or_edit_main_menu(update, context, "❌ Invalid or expired promo code.")
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")

    return await _show_time_slots(update, context)


async def _show_time_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """U-020: Delivery time-slot picker."""
    kb = []
    now = int(time.time())
    day_start = now - (now % 86400)
    slots = [
        ("Today 2–4 PM",   day_start + 14 * 3600, day_start + 16 * 3600),
        ("Today 4–6 PM",   day_start + 16 * 3600, day_start + 18 * 3600),
        ("Today 6–8 PM",   day_start + 18 * 3600, day_start + 20 * 3600),
        ("Tomorrow 10–12", day_start + 86400 + 10 * 3600, day_start + 86400 + 12 * 3600),
        ("Tomorrow 2–4 PM",day_start + 86400 + 14 * 3600, day_start + 86400 + 16 * 3600),
        ("ASAP",           0, 0),
    ]
    for label, start, end in slots:
        kb.append([InlineKeyboardButton(
            f"🕐 {label}",
            callback_data=f"slot:{start}:{end}",
        )])
    await _safe_reply(update, context, "🕐 *Choose a delivery time:*",
                      reply_markup=InlineKeyboardMarkup(kb))
    return STATE_SLOT


async def cb_slot_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, start_str, end_str = query.data.split(":")
    context.user_data["slot_start"] = int(start_str)
    context.user_data["slot_end"] = int(end_str)
    return await _show_order_confirm(update, context)


async def _show_order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show final confirmation with contactless option."""
    quote: pb.QuoteResponse = context.user_data.get("quote")
    if not quote:
        await send_or_edit_main_menu(update, context, "⚠️ Session expired. Please start checkout again.")
        return ConversationHandler.END

    kb = [
        [InlineKeyboardButton("✅ Confirm Order", callback_data="confirm_order")],
        [InlineKeyboardButton("🚪 Contactless delivery", callback_data="confirm_order_contactless")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")],
    ]
    text = (
        "📋 *Order Confirmation*\n\n"
        + quote.formatted_receipt
        + f"\n\n📍 To: {context.user_data.get('delivery_address', 'N/A')}"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await send_or_edit_main_menu(update, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return STATE_CONFIRM


async def cb_confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Place the order."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    items = await _cart(context).get_cart(user.id)
    quote: pb.QuoteResponse = context.user_data.get("quote")

    if not quote or not items:
        await send_or_edit_main_menu(update, context, "⚠️ Session expired. Please start checkout again with /cart")
        return ConversationHandler.END

    contactless = query.data == "confirm_order_contactless"

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/PlaceOrder",
            lambda stub, meta: stub.PlaceOrder(
                pb.OrderRequest(
                    user_id=user.id,
                    username=user.username or user.first_name,
                    quote_data=quote.quote_data,
                    expected_total=quote.grand_total,
                    receipt_text=quote.formatted_receipt,
                    idempotency_key=hashlib.sha256(quote.quote_data).hexdigest(),
                    delivery_address=context.user_data.get("delivery_address", ""),
                    scheduled_for=context.user_data.get("slot_start", 0),
                    contactless=contactless,
                    order_notes=context.user_data.get("order_notes", ""),
                ),
                metadata=meta,
            ),
            user_id=user.id,
        )
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ Could not place order: {e.details()}")
        return ConversationHandler.END

    await _cart(context).clear(user.id)

    confirmation = (
        f"🎉 *Order Placed!*\n\n"
        f"*{resp.human_ref}*\n\n"
        f"{quote.formatted_receipt}\n\n"
        f"🔐 Delivery OTP: *check /orders for your confirmation code*\n\n"
        f"We'll send you updates as your order progresses."
    )
    if quote.loyalty_points_earned > 0:
        confirmation += f"\n\n🏅 You earned *{quote.loyalty_points_earned} loyalty points!*"

    await send_or_edit_main_menu(update, context, confirmation, parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear()
    return ConversationHandler.END
