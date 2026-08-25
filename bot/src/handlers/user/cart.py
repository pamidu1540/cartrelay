"""
cart.py — Shopping cart view, quantity adjustments, item removal, and clearing.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import check_maintenance, rate_limit
from handlers.user.common import _cart, _safe_reply

logger = logging.getLogger(__name__)


# ── Cart Handlers ─────────────────────────────────────────────────────────────

@rate_limit
@check_maintenance
async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    """Show cart with +/− buttons and checkout option."""
    user_id = update.effective_user.id
    return await _show_cart(update, context, user_id)


async def _show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    items = await _cart(context).get_cart(user_id)

    if not items:
        await _safe_reply(
            update,
            context,
            "🛒 Your cart is empty.\n\nUse /catalog to browse items.",
        )
        return ConversationHandler.END

    try:
        async def _fetch_it(it_id):
            try:
                return await pool.call(
                    "/logistics.LogisticsEngine/GetItem",
                    lambda stub, meta: stub.GetItem(pb.ItemId(id=it_id), metadata=meta),
                )
            except Exception:
                return None

        item_objs = await asyncio.gather(*(_fetch_it(ci.item_id) for ci in items))
        item_map = {it.id: it for it in item_objs if it is not None}
    except Exception:
        item_map = {}

    lines = ["🛒 *Your Cart*\n"]
    subtotal = 0.0
    kb = []

    for ci in items:
        it = item_map.get(ci.item_id)
        name = it.name if it else ci.item_id
        price = it.price if it else 0.0
        line_total = price * ci.quantity
        subtotal += line_total

        lines.append(f"• {name} × {ci.quantity}  —  LKR {line_total:.0f}")
        kb.append([
            InlineKeyboardButton("➖", callback_data=f"qty_minus:{ci.item_id}"),
            InlineKeyboardButton(f"{ci.quantity}×  {name[:20]}", callback_data=f"item_detail:{ci.item_id}"),
            InlineKeyboardButton("➕", callback_data=f"qty_plus:{ci.item_id}"),
            InlineKeyboardButton("🗑", callback_data=f"cart_remove:{ci.item_id}"),
        ])

    lines.append(f"\n*Subtotal: LKR {subtotal:.0f}*")

    try:
        settings = await pool.call(
            "/logistics.LogisticsEngine/GetSystemSettings",
            lambda stub, meta: stub.GetSystemSettings(pb.Empty(), metadata=meta),
        )
        threshold = settings.free_delivery_threshold
        if 0 < threshold > subtotal:
            gap = threshold - subtotal
            lines.append(f"🎁 Add LKR {gap:.0f} more for FREE delivery!")
    except Exception:
        pass

    kb.append([
        InlineKeyboardButton("🗑 Clear Cart", callback_data="cart_clear"),
        InlineKeyboardButton("✅ Checkout",  callback_data="checkout"),
    ])

    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    return ConversationHandler.END


async def cb_qty_plus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id
    await _cart(context).add_item(user_id, item_id, 1)
    await _refresh_cart_message(query, context, user_id)


async def cb_qty_minus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id
    items = await _cart(context).get_cart(user_id)
    current = next((ci.quantity for ci in items if ci.item_id == item_id), 0)
    if current <= 1:
        await _cart(context).remove_item(user_id, item_id)
    else:
        await _cart(context).add_item(user_id, item_id, -1)
    await _refresh_cart_message(query, context, user_id)


async def cb_remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id
    await _cart(context).remove_item(user_id, item_id)
    await query.answer("Item removed from cart")
    await _refresh_cart_message(query, context, user_id)


async def cb_clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _cart(context).clear(update.effective_user.id)
    await send_or_edit_main_menu(update, context, "🛒 Cart cleared.")


async def _refresh_cart_message(query, context, user_id: int) -> None:
    """Re-render the cart inline message after a mutation."""
    items = await _cart(context).get_cart(user_id)
    if not items:
        await query.edit_message_text("🛒 Your cart is empty.")
        return

    try:
        async def _fetch_it(it_id):
            try:
                return await pool.call(
                    "/logistics.LogisticsEngine/GetItem",
                    lambda stub, meta: stub.GetItem(pb.ItemId(id=it_id), metadata=meta),
                )
            except Exception:
                return None

        item_objs = await asyncio.gather(*(_fetch_it(ci.item_id) for ci in items))
        item_map = {it.id: it for it in item_objs if it is not None}
    except Exception:
        item_map = {}

    lines = ["🛒 *Your Cart*\n"]
    subtotal = 0.0
    kb = []
    for ci in items:
        it = item_map.get(ci.item_id)
        name = it.name if it else ci.item_id
        price = it.price if it else 0.0
        line_total = price * ci.quantity
        subtotal += line_total
        lines.append(f"• {name} × {ci.quantity}  —  LKR {line_total:.0f}")
        kb.append([
            InlineKeyboardButton("➖", callback_data=f"qty_minus:{ci.item_id}"),
            InlineKeyboardButton(f"{ci.quantity}× {name[:20]}", callback_data=f"item_detail:{ci.item_id}"),
            InlineKeyboardButton("➕", callback_data=f"qty_plus:{ci.item_id}"),
            InlineKeyboardButton("🗑", callback_data=f"cart_remove:{ci.item_id}"),
        ])
    lines.append(f"\n*Subtotal: LKR {subtotal:.0f}*")
    kb.append([
        InlineKeyboardButton("🗑 Clear", callback_data="cart_clear"),
        InlineKeyboardButton("✅ Checkout", callback_data="checkout"),
    ])
    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )
