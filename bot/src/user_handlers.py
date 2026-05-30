"""
user_handlers.py — all customer-facing Telegram handlers
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cart_manager import CartManager

import grpc
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import check_maintenance, rate_limit

if TYPE_CHECKING:
    from cart_manager import CartManager

logger = logging.getLogger(__name__)

# ConversationHandler states (imported from main.py at runtime via context)
(
    STATE_ADDRESS, STATE_PROMO, STATE_SLOT, STATE_CONFIRM,
    STATE_ITEM_NAME, STATE_ITEM_PRICE, STATE_ITEM_STOCK, STATE_ITEM_CATEGORY,
    STATE_ITEM_PHOTO, STATE_ITEM_DESC, STATE_STATUS_REASON, STATE_NOTE_TEXT,
    STATE_ORDER_SEARCH, STATE_WH_NAME, STATE_WH_COORDS, STATE_DRV_NAME,
    STATE_DRV_PHONE, STATE_PROMO_DEF, STATE_BROADCAST, STATE_LOYALTY_ADJ,
    STATE_NEW_ADMIN, STATE_SETTINGS_VAL, STATE_MAINT_WINDOW, STATE_RESOLVE_NOTE,
    STATE_PROFILE_NAME, STATE_PROFILE_PHONE, STATE_NEW_ADDRESS, STATE_PROMO_CODE,
    STATE_CSV_UPLOAD, STATE_EDIT_ITEM_ID, STATE_EDIT_ITEM_PHOTO,
) = range(31)

PAGE_SIZE = 10  # items per catalog page


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cart(context: ContextTypes.DEFAULT_TYPE) -> CartManager:
    return context.application.bot_data["cart_manager"]


async def _update_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    # Try to delete the user's incoming message first to keep the chat clean
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    chat_id = update.effective_chat.id
    # Check if we have a last bot message to edit
    last_msg_id = context.user_data.get("last_bot_msg_id")
    if last_msg_id:
        try:
            # If the update has a callback query, we can edit the message directly
            if update.callback_query:
                msg = await update.callback_query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
                context.user_data["last_bot_msg_id"] = msg.message_id
                return msg
            else:
                msg = await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=last_msg_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
                return msg
        except Exception:
            # If editing fails, delete it and send a new one
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
            except Exception:
                pass

    # Send a new message and save its ID
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )
    context.user_data["last_bot_msg_id"] = msg.message_id
    return msg


async def _safe_reply(update: Update, text: str, **kwargs) -> None:
    """Reply with parse_mode=Markdown, gracefully handle long messages."""
    msg = update.effective_message
    if not msg:
        return
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)


def _order_status_emoji(status: str) -> str:
    return {
        "pending":   "🕐",
        "confirmed": "✅",
        "dispatched":"🚗",
        "delivered": "📦",
        "cancelled": "❌",
        "failed":    "⚠️",
    }.get(status, "❓")


# ── /start ────────────────────────────────────────────────────────────────────

@rate_limit
@check_maintenance
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """BUG-030: reset any stale conversation state. BUG-016: invalidate cart cache."""
    user = update.effective_user
    user_id = user.id
    await _cart(context).invalidate_local(user_id)
    
    # Try to delete incoming /start command message to keep chat tidy
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    # 1. Check if user profile is registered
    profile = None
    try:
        profile = await pool.call(
            "/logistics.LogisticsEngine/GetProfile",
            lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
            user_id=user_id
        )
    except Exception:
        pass

    if not profile or not profile.display_name or not profile.phone:
        # Prompt user to set up profile
        context.user_data["registering"] = True
        context.user_data["reg_profile"] = {"user_id": user_id}
        
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👋 *Welcome to Logistics Engine!*\n\nLet's set up your profile first.\n\n✏️ *Step 1:* Please enter your full display name:",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return STATE_PROFILE_NAME

    # 2. Check if user has a default address configured
    has_address = False
    try:
        addr_resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(pb.UserId(user_id=user_id), metadata=meta),
            user_id=user_id
        )
        if addr_resp.addresses:
            has_address = True
    except Exception:
        pass

    if not has_address:
        context.user_data["registering"] = True
        reply_kb = [
            [KeyboardButton("📍 Share Location", request_location=True)],
        ]
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "📍 *Delivery Address Required*\n\n"
                "An address is required to calculate delivery distances and browse catalog items.\n\n"
                "👉 Please tap *Share Location* below to share your GPS location, or type your address manually:"
            ),
            reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return STATE_NEW_ADDRESS

    # 3. Both profile and address exist — load standard main menu
    disclaimer = ""
    try:
        wh_resp = await pool.call(
            "/logistics.LogisticsEngine/ListWarehouses",
            lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta)
        )
        has_wh = len(wh_resp.warehouses) > 0
    except Exception:
        has_wh = False

    try:
        cat_resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(pb.CatalogRequest(page=0, page_size=1), metadata=meta)
        )
        has_cat = len(cat_resp.items) > 0
    except Exception:
        has_cat = False

    if not has_wh or not has_cat:
        disclaimer = (
            "⚠️ *Disclaimer:* The system is currently in initial setup mode. "
            "Admin options (such as warehouses or catalog items) have not been fully configured yet.\n\n"
        )

    kb = [
        [InlineKeyboardButton("🛒 Browse Catalog", callback_data="cat_select:all:0")],
        [InlineKeyboardButton("🛍 My Cart",   callback_data="checkout"),
         InlineKeyboardButton("📋 My Orders", callback_data="order_detail:list")],
        [InlineKeyboardButton("👤 Profile",   callback_data="profile_edit:view"),
         InlineKeyboardButton("🎁 Points",    callback_data="loyalty_view")],
        [InlineKeyboardButton("❓ Help",       callback_data="help")],
    ]
    
    # Try deleting previous bot message before sending main menu
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
    except Exception:
        pass

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{disclaimer}👋 Welcome back, *{user.first_name}*!\n\nHow can we help you today?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["last_bot_msg_id"] = msg.message_id
    return ConversationHandler.END


@rate_limit
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-045: FAQ / help section."""
    text = (
        "❓ *Help & FAQ*\n\n"
        "*How to order*\n"
        "Use /catalog to browse items, add them to your cart, then /cart to checkout.\n\n"
        "*Track my delivery*\n"
        "Use /orders to see your current order status and ETA updates.\n\n"
        "*Cancel an order*\n"
        "Use /orders → tap the order → Cancel (within 5 minutes of placing).\n\n"
        "*Contact support*\n"
        "Use /support to open a live support chat with our team.\n\n"
        "*Promo codes*\n"
        "Enter your promo code at checkout when prompted.\n\n"
        "*Loyalty points*\n"
        "Earn points on every order. Check your balance with /points."
    )
    await _safe_reply(update, text)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_reply(update, "Operation cancelled.")
    return ConversationHandler.END


# ── Catalog ───────────────────────────────────────────────────────────────────

@rate_limit
@check_maintenance
async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-011: Show category buttons for browsing."""
    # Try deleting incoming message
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    # Try deleting previous bot message
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
    except Exception:
        pass

    # Check setup status
    disclaimer = ""
    try:
        wh_resp = await pool.call(
            "/logistics.LogisticsEngine/ListWarehouses",
            lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta)
        )
        has_wh = len(wh_resp.warehouses) > 0
    except Exception:
        has_wh = False

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(page=0, page_size=200), metadata=meta
            ),
        )
        has_cat = len(resp.items) > 0
    except grpc.RpcError as e:
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Could not load catalog: {e.details()}"
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return

    if not has_wh or not has_cat:
        disclaimer = (
            "⚠️ *Disclaimer:* The system is currently in initial setup mode. "
            "Admin options (such as warehouses or catalog items) have not been fully configured/enabled yet. "
            "Some features may be limited.\n\n"
        )

    # Build category list
    categories = sorted({it.category for it in resp.items if it.is_available})
    kb = [[InlineKeyboardButton(f"📂 {cat}", callback_data=f"cat_select:{cat}:0")]
          for cat in categories]
    kb.append([InlineKeyboardButton("🔍 All Items", callback_data="cat_select:all:0")])
    kb.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="profile_edit:back_to_main")])
    
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{disclaimer}🛒 *Browse our catalog*\nChoose a category:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["last_bot_msg_id"] = msg.message_id


async def cb_category_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle category selection and show first page of items."""
    query = update.callback_query
    await query.answer()
    _, cat, page_str = query.data.split(":", 2)
    page = int(page_str)
    cat_filter = "" if cat == "all" else cat
    await _show_catalog_page(query, cat_filter, page)


async def cb_catalog_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pagination Next/Prev."""
    query = update.callback_query
    await query.answer()
    _, cat, page_str = query.data.split(":", 2)
    page = int(page_str)
    cat_filter = "" if cat == "all" else cat
    await _show_catalog_page(query, cat_filter, page)


async def _show_catalog_page(query, cat_filter: str, page: int) -> None:
    """U-019 / BUG-019: paginated catalog, 10 items per page."""
    # Check if the message is a photo message. If so, delete it and send a new text message.
    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        # Send a new text message to use as the base for the catalog page
        query = await query.bot.send_message(
            chat_id=query.message.chat_id,
            text="⌛ Loading catalog..."
        )

    # Universal edit helper
    async def edit_msg(text, **kwargs):
        chat_id = query.message.chat_id if hasattr(query, "message") and query.message is not None else query.chat_id
        message_id = query.message.message_id if hasattr(query, "message") and query.message is not None else query.message_id
        return await query.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(
                    category_filter=cat_filter,
                    page=page,
                    page_size=PAGE_SIZE,
                ),
                metadata=meta,
            ),
        )
    except grpc.RpcError as e:
        await edit_msg(f"⚠️ Error: {e.details()}")
        return

    if not resp.items:
        await edit_msg("No items found in this category.")
        return

    cat_label = cat_filter or "all"
    lines = [f"🛍 *{cat_filter or 'All Items'}* — page {page + 1}\n"]
    kb = []

    for it in resp.items:
        stock_label = f"({it.stock} left)" if it.stock <= 5 else ""
        lines.append(f"• {it.name}  —  LKR {it.price:.0f} {stock_label}")
        kb.append([InlineKeyboardButton(
            f"{'🔴 ' if it.stock == 0 else ''}{it.name}",
            callback_data=f"item_detail:{it.id}",
        )])

    # Pagination buttons
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"cat_page:{cat_label}:{page - 1}"))
    total_pages = (resp.total_count + PAGE_SIZE - 1) // PAGE_SIZE
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"cat_page:{cat_label}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="profile_edit:back_to_catalog")])
    kb.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="profile_edit:back_to_main")])

    await edit_msg(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cb_item_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-013: Full item detail view."""
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]

    try:
        item = await pool.call(
            "/logistics.LogisticsEngine/GetItem",
            lambda stub, meta: stub.GetItem(pb.ItemId(id=item_id), metadata=meta),
        )
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ {e.details()}")
        return

    stock_text = (
        "✅ In Stock" if item.stock > 5
        else f"⚠️ Only {item.stock} left!" if item.stock > 0
        else "❌ Out of Stock"
    )

    user_id = update.effective_user.id
    distance_info = ""
    try:
        # Fetch user's default saved address
        addr_resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(pb.UserId(user_id=user_id), metadata=meta),
            user_id=user_id
        )
        default_addr = next((a for a in addr_resp.addresses if a.is_default), None)
        
        # Fetch active warehouses
        wh_resp = await pool.call(
            "/logistics.LogisticsEngine/ListWarehouses",
            lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta)
        )
        active_warehouses = [w for w in wh_resp.warehouses if w.is_active]
        
        if not default_addr:
            distance_info = "📍 _Set up address in Profile to see delivery distance._"
        elif default_addr.lat == 0.0 or default_addr.lon == 0.0:
            distance_info = "📍 _Distance unavailable. Share location in Profile for exact distance._"
        elif not active_warehouses:
            distance_info = "📍 _No active warehouses found for delivery calculation._"
        else:
            import math
            min_dist = float('inf')
            nearest_wh = None
            for w in active_warehouses:
                if w.lat != 0.0 or w.lon != 0.0:
                    R = 6371.0
                    phi1 = math.radians(default_addr.lat)
                    phi2 = math.radians(w.lat)
                    delta_phi = math.radians(w.lat - default_addr.lat)
                    delta_lambda = math.radians(w.lon - default_addr.lon)
                    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
                    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
                    dist = R * c
                    if dist < min_dist:
                        min_dist = dist
                        nearest_wh = w
            if nearest_wh:
                distance_info = f"📍 *Nearest Depot:* {nearest_wh.name} ({min_dist:.1f} km)"
            else:
                distance_info = "📍 _Nearest Depot: Location coordinate mismatch._"
    except Exception as e:
        logger.error(f"Error calculating distance: {e}")
        distance_info = "📍 _Distance calculation unavailable._"

    caption = (
        f"*{item.name}*\n\n"
        f"{item.description}\n\n"
        f"💰 LKR {item.price:.0f}\n"
        f"📦 {stock_text}\n"
        f"{distance_info}"
    )
    if item.is_featured:
        caption = "⭐ *Featured*\n" + caption

    kb: list[list[InlineKeyboardButton]] = []
    if item.stock > 0:
        kb.append([
            InlineKeyboardButton("➕ Add to Cart", callback_data=f"add_to_cart:{item_id}:1"),
        ])
    else:
        # U-014: Out-of-stock notify
        kb.append([InlineKeyboardButton(
            "🔔 Notify me when available",
            callback_data=f"stock_notify:{item_id}",
        )])

    kb.append([InlineKeyboardButton("⬅ Back to Catalog", callback_data=f"cat_select:{item.category or 'all'}:0")])

    if item.photo_file_id:
        try:
            await query.message.delete()
        except Exception:
            pass
        msg = await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=item.photo_file_id,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
    else:
        await query.edit_message_text(
            caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        context.user_data["last_bot_msg_id"] = query.message.message_id


async def cb_add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add item to cart and confirm."""
    query = update.callback_query
    await query.answer()
    _, item_id, qty_str = query.data.split(":")
    user_id = update.effective_user.id

    items = await _cart(context).add_item(user_id, item_id, int(qty_str))
    total_items = sum(ci.quantity for ci in items)
    in_cart = sum(ci.quantity for ci in items if ci.item_id == item_id)

    kb = [
        [
            InlineKeyboardButton(f"➕ Add More ({in_cart} in cart)", callback_data=f"add_to_cart:{item_id}:1"),
        ],
        [
            InlineKeyboardButton("🛍 My Cart", callback_data="checkout"),
            InlineKeyboardButton("⬅ Back to Catalog", callback_data="cat_select:all:0"),
        ]
    ]
    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass
    await query.answer(f"✅ Added to cart ({total_items} items total)", show_alert=False)


async def cb_stock_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-014: Subscribe to restock notification."""
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        await pool.call(
            "/logistics.LogisticsEngine/SubscribeStock",
            lambda stub, meta: stub.SubscribeStock(
                pb.StockSubRequest(user_id=user_id, item_id=item_id),
                metadata=meta,
            ),
            user_id=user_id,
        )
        await query.answer("🔔 We'll notify you when this item is back in stock!", show_alert=True)
    except grpc.RpcError as e:
        await query.answer(f"⚠️ {e.details()}", show_alert=True)


# ── Cart ──────────────────────────────────────────────────────────────────────

@rate_limit
@check_maintenance
async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show cart with +/− buttons and checkout option."""
    user_id = update.effective_user.id
    return await _show_cart(update, context, user_id)


async def _show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    items = await _cart(context).get(user_id)

    if not items:
        await _safe_reply(
            update,
            "🛒 Your cart is empty.\n\nUse /catalog to browse items.",
        )
        return ConversationHandler.END

    # Fetch item details for display
    try:
        catalog_resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(page=0, page_size=200), metadata=meta
            ),
        )
        item_map = {it.id: it for it in catalog_resp.items}
    except grpc.RpcError:
        item_map = {}

    # U-003: Itemised cart summary
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
        # U-016: +/− quantity buttons
        kb.append([
            InlineKeyboardButton("➖", callback_data=f"qty_minus:{ci.item_id}"),
            InlineKeyboardButton(f"{ci.quantity}×  {name[:20]}", callback_data=f"item_detail:{ci.item_id}"),
            InlineKeyboardButton("➕", callback_data=f"qty_plus:{ci.item_id}"),
            InlineKeyboardButton("🗑", callback_data=f"cart_remove:{ci.item_id}"),
        ])

    lines.append(f"\n*Subtotal: LKR {subtotal:.0f}*")

    # U-042: Free delivery indicator
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
    items = await _cart(context).get(user_id)
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
    await query.edit_message_text("🛒 Cart cleared.")


async def _refresh_cart_message(query, context, user_id: int) -> None:
    """Re-render the cart inline message after a mutation."""
    items = await _cart(context).get(user_id)
    if not items:
        await query.edit_message_text("🛒 Your cart is empty.")
        return
    # Re-use cmd_cart logic — simplified for inline edit
    try:
        catalog_resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(page=0, page_size=200), metadata=meta
            ),
        )
        item_map = {it.id: it for it in catalog_resp.items}
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


# ── Checkout flow ─────────────────────────────────────────────────────────────

async def cb_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start checkout: ask for delivery address or show saved addresses."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    items = await _cart(context).get(user_id)
    if not items:
        await query.edit_message_text("🛒 Your cart is empty. Use /catalog to add items.")
        return ConversationHandler.END

    # U-030: load saved addresses
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
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return STATE_ADDRESS

    # Load the saved address
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
    """Store typed address/location and proceed to quote."""
    msg = update.effective_message
    user_id = update.effective_user.id
    
    # Try deleting user incoming message
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
        
        # Save as a default address
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
    
    # Remove old bot message
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
    except Exception:
        pass
        
    return await _proceed_to_quote(update, context)


async def _proceed_to_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Calculate quote and show promo / loyalty options."""
    user_id = update.effective_user.id
    items = await _cart(context).get(user_id)

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
        await _safe_reply(update, f"⚠️ Could not calculate quote: {e.details()}")
        return ConversationHandler.END

    context.user_data["quote"] = quote

    kb = [
        [InlineKeyboardButton("🏷 Enter Promo Code", callback_data="promo:enter")],
        [InlineKeyboardButton("⏩ Skip Promo",       callback_data="promo:skip")],
    ]

    # U-039: show loyalty balance
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
        + f"\n🕐 Est. delivery: ~{int(quote.distance_km * 3 + 15)} min"  # U-052: ETA estimate
    )
    await _safe_reply(update, text, reply_markup=InlineKeyboardMarkup(kb))
    return STATE_PROMO


async def cb_use_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "promo:enter":
        await query.edit_message_text("🏷 Enter your promo code:")
        return STATE_PROMO_CODE

    if query.data.startswith("promo:loyalty:"):
        pts = int(query.data.split(":")[-1])
        context.user_data["loyalty_redeem"] = pts
        await query.answer(f"✅ {pts} loyalty points will be applied at checkout")

    return await _show_time_slots(update, context)


async def msg_promo_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.effective_message.text.strip().upper()
    user_id = update.effective_user.id
    items = await _cart(context).get(user_id)

    try:
        # Recalculate quote with promo
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
            await _safe_reply(update,
                f"✅ Promo *{code}* applied! You save LKR {quote.promo_discount:.0f}")
        else:
            await _safe_reply(update, "❌ Invalid or expired promo code.")
    except grpc.RpcError as e:
        await _safe_reply(update, f"⚠️ {e.details()}")

    return await _show_time_slots(update, context)


async def _show_time_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """U-020: Delivery time-slot picker."""
    kb = []
    # Generate simple time slots for today/tomorrow
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
    await _safe_reply(update, "🕐 *Choose a delivery time:*",
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
        await _safe_reply(update, "⚠️ Session expired. Please start checkout again.")
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
        await _safe_reply(update, text, reply_markup=InlineKeyboardMarkup(kb))
    return STATE_CONFIRM


async def cb_confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Place the order."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    items = await _cart(context).get(user.id)
    quote: pb.QuoteResponse = context.user_data.get("quote")

    if not quote or not items:
        await query.edit_message_text("⚠️ Session expired. Please start checkout again with /cart")
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
                    idempotency_key=str(uuid.uuid4()),  # BUG-012
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
        await query.edit_message_text(f"⚠️ Could not place order: {e.details()}")
        return ConversationHandler.END

    await _cart(context).clear(user.id)

    # U-009: prominent order number + U-026: OTP
    confirmation = (
        f"🎉 *Order Placed!*\n\n"
        f"*{resp.human_ref}*\n\n"
        f"{quote.formatted_receipt}\n\n"
        f"🔐 Delivery OTP: *check /orders for your confirmation code*\n\n"
        f"We'll send you updates as your order progresses."
    )
    if quote.loyalty_points_earned > 0:
        confirmation += f"\n\n🏅 You earned *{quote.loyalty_points_earned} loyalty points!*"

    await query.edit_message_text(confirmation, parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear()
    return ConversationHandler.END


# ── Order history ─────────────────────────────────────────────────────────────

@rate_limit
async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-001: Paginated order history."""
    user_id = update.effective_user.id
    page = context.user_data.get("orders_page", 0)

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetUserOrders",
            lambda stub, meta: stub.GetUserOrders(
                pb.GetUserOrdersRequest(user_id=user_id, page=page, page_size=5),
                metadata=meta,
            ),
            user_id=user_id,
        )
    except grpc.RpcError as e:
        await _safe_reply(update, f"⚠️ Could not load orders: {e.details()}")
        return

    if not resp.orders:
        await _safe_reply(update, "📋 You have no orders yet.\n\nUse /catalog to place your first order!")
        return

    lines = [f"📋 *My Orders* (page {page + 1})\n"]
    kb = []
    for o in resp.orders:
        emoji = _order_status_emoji(o.status)
        lines.append(f"{emoji} *{o.human_ref}* — LKR {o.total:.0f} — {o.status}")
        kb.append([InlineKeyboardButton(
            f"{emoji} {o.human_ref}  LKR {o.total:.0f}",
            callback_data=f"order_detail:{o.order_id}",
        )])

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Older", callback_data=f"orders_page:{page - 1}"))
    total_pages = (resp.total_count + 4) // 5
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Newer ➡", callback_data=f"orders_page:{page + 1}"))
    if nav:
        kb.append(nav)

    await _safe_reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a single order with Reorder and Cancel buttons."""
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]

    try:
        order = await pool.call(
            "/logistics.LogisticsEngine/GetOrder",
            lambda stub, meta: stub.GetOrder(
                pb.OrderId(order_id=order_id), metadata=meta
            ),
        )
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ {e.details()}")
        return

    emoji = _order_status_emoji(order.status)
    lines = [
        f"{emoji} *{order.human_ref}*",
        f"Status: {order.status.title()}",
        f"Total: LKR {order.total:.0f}",
        f"Address: {order.delivery_address}",
    ]
    if order.delivery_otp and order.status == "dispatched":
        lines.append(f"\n🔐 Delivery OTP: *{order.delivery_otp}*")
    if order.order_notes:
        lines.append(f"Notes: _{order.order_notes}_")

    kb = []
    # U-002: Reorder
    kb.append([InlineKeyboardButton("🔄 Reorder", callback_data=f"reorder:{order_id}")])
    # U-007: Cancel within window
    if order.status == "pending":
        kb.append([InlineKeyboardButton("❌ Cancel Order", callback_data=f"confirm_cancel:{order_id}")])
    # U-034: Report issue
    if order.status == "delivered":
        kb.append([InlineKeyboardButton("⚠️ Report Issue", callback_data=f"issue_type:{order_id}")])

    kb.append([InlineKeyboardButton("⬅ Back to Orders", callback_data="orders_list")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cb_reorder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-002: Pre-fill cart with items from a past order."""
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        order = await pool.call(
            "/logistics.LogisticsEngine/GetOrder",
            lambda stub, meta: stub.GetOrder(
                pb.OrderId(order_id=order_id), metadata=meta
            ),
        )
    except grpc.RpcError as e:
        await query.answer(f"⚠️ {e.details()}", show_alert=True)
        return

    # Parse receipt to extract items
    import re
    parsed_items = []
    for line in order.receipt.splitlines():
        line = line.strip()
        if line.startswith("• "):
            match = re.match(r"^•\s+(.+?)\s+×\s+(\d+)\s+—\s+LKR", line)
            if match:
                name = match.group(1).strip()
                qty = int(match.group(2))
                parsed_items.append((name, qty))

    if not parsed_items:
        await query.answer("❌ Could not extract items from past order.", show_alert=True)
        return

    # Fetch catalog to map names to IDs
    try:
        catalog_resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(page=0, page_size=1000), metadata=meta
            ),
        )
        name_to_id = {it.name.strip().lower(): it.id for it in catalog_resp.items}
    except Exception as e:
        await query.answer(f"⚠️ Could not load catalog: {e}", show_alert=True)
        return

    added_count = 0
    for name, qty in parsed_items:
        item_id = name_to_id.get(name.lower())
        if item_id:
            await _cart(context).add_item(user_id, item_id, qty)
            added_count += 1

    if added_count == 0:
        await query.answer("❌ None of the items from this order are currently available.", show_alert=True)
        return

    await query.answer("Cart pre-filled from your previous order!", show_alert=True)
    await query.edit_message_text(
        f"✅ {added_count} item(s) from *{order.human_ref}* successfully added to your cart.\n\nUse /cart to review and checkout.",
        parse_mode=ParseMode.MARKDOWN,
    )


@rate_limit
async def cmd_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_reply(update, "Please use /orders to find and cancel an order.")


async def cb_confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/CancelOrder",
            lambda stub, meta: stub.CancelOrder(
                pb.CancelOrderRequest(
                    order_id=order_id,
                    user_id=user_id,
                    reason="Customer requested cancellation",
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )
        await query.edit_message_text(
            f"✅ {resp.message}" if resp.success else f"❌ {resp.message}"
        )
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ {e.details()}")


async def cmd_report_issue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_reply(update, "Please use /orders, tap the delivered order, then tap ⚠️ Report Issue.")


async def cb_issue_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]
    context.user_data["issue_order_id"] = order_id

    kb = [
        [InlineKeyboardButton("Wrong item",     callback_data=f"issue_submit:{order_id}:wrong_item")],
        [InlineKeyboardButton("Damaged item",   callback_data=f"issue_submit:{order_id}:damaged")],
        [InlineKeyboardButton("Missing item",   callback_data=f"issue_submit:{order_id}:missing")],
        [InlineKeyboardButton("Other issue",    callback_data=f"issue_submit:{order_id}:other")],
    ]
    await query.edit_message_text(
        "⚠️ *What went wrong?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Profile ───────────────────────────────────────────────────────────────────

@rate_limit
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Try deleting incoming message
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    # Try deleting previous bot message
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
    except Exception:
        pass

    user_id = update.effective_user.id
    try:
        profile = await pool.call(
            "/logistics.LogisticsEngine/GetProfile",
            lambda stub, meta: stub.GetProfile(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        profile = pb.CustomerProfile(user_id=user_id)

    name = profile.display_name or "Not set"
    phone = profile.phone or "Not set"
    lang = profile.language or "en"

    kb = [
        [InlineKeyboardButton("✏️ Edit Name",  callback_data="profile_edit:name")],
        [InlineKeyboardButton("📱 Edit Phone", callback_data="profile_edit:phone")],
        [InlineKeyboardButton("📍 Addresses",  callback_data="profile_edit:addresses")],
        [InlineKeyboardButton("⬅ Back to Menu", callback_data="profile_edit:back_to_main")],
    ]
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"👤 *Your Profile*\n\nName: {name}\nPhone: {phone}\nLanguage: {lang}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["last_bot_msg_id"] = msg.message_id


async def _show_catalog_categories_helper(query) -> None:
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(page=0, page_size=200), metadata=meta
            ),
        )
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ Could not load catalog: {e.details()}")
        return

    categories = sorted({it.category for it in resp.items if it.is_available})
    kb = [[InlineKeyboardButton(f"📂 {cat}", callback_data=f"cat_select:{cat}:0")]
          for cat in categories]
    kb.append([InlineKeyboardButton("🔍 All Items", callback_data="cat_select:all:0")])
    kb.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="profile_edit:back_to_main")])
    
    chat_id = query.message.chat_id if hasattr(query, "message") and query.message is not None else query.chat_id
    message_id = query.message.message_id if hasattr(query, "message") and query.message is not None else query.message_id
    
    await query.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text="🛒 *Browse our catalog*\nChoose a category:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_profile_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]

    if field == "view":
        user_id = update.effective_user.id
        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(
                    pb.UserId(user_id=user_id), metadata=meta
                ),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        name = profile.display_name or "Not set"
        phone = profile.phone or "Not set"
        lang = profile.language or "en"

        kb = [
            [InlineKeyboardButton("✏️ Edit Name",  callback_data="profile_edit:name")],
            [InlineKeyboardButton("📱 Edit Phone", callback_data="profile_edit:phone")],
            [InlineKeyboardButton("📍 Addresses",  callback_data="profile_edit:addresses")],
            [InlineKeyboardButton("⬅ Back to Menu", callback_data="profile_edit:back_to_main")],
        ]
        await query.edit_message_text(
            f"👤 *Your Profile*\n\nName: {name}\nPhone: {phone}\nLanguage: {lang}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return ConversationHandler.END

    if field == "back_to_catalog":
        # Check if the message is a photo message. If so, delete it and send a new text message.
        if query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            # Send a new text message to use as the base
            query = await query.bot.send_message(
                chat_id=query.message.chat_id,
                text="⌛ Loading catalog..."
            )
        await _show_catalog_categories_helper(query)
        return ConversationHandler.END

    if field == "back_to_main":
        disclaimer = ""
        if not pool.is_licensed():
            disclaimer = (
                "⚠️ *Running in UNLICENSED mode.*\n"
                "Some features may be limited.\n\n"
            )

        kb = [
            [InlineKeyboardButton("🛒 Browse Catalog", callback_data="cat_select:all:0")],
            [InlineKeyboardButton("🛍 My Cart",   callback_data="checkout"),
             InlineKeyboardButton("📋 My Orders", callback_data="order_detail:list")],
            [InlineKeyboardButton("👤 Profile",   callback_data="profile_edit:view"),
             InlineKeyboardButton("🎁 Points",    callback_data="loyalty_view")],
            [InlineKeyboardButton("❓ Help",       callback_data="help")],
        ]
        await query.edit_message_text(
            f"{disclaimer}👋 Welcome back, *{update.effective_user.first_name}*!\n\nHow can we help you today?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return ConversationHandler.END

    if field == "name":
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="profile_edit:view")]]
        await query.edit_message_text("✏️ Enter your new display name:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROFILE_NAME

    if field == "phone":
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="profile_edit:view")]]
        await query.edit_message_text("📱 Enter your phone number:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROFILE_PHONE

    if field == "addresses":
        user_id = update.effective_user.id
        await _edit_address_list(query, user_id)
        return ConversationHandler.END

    if field == "add_address":
        try:
            await query.message.delete()
        except Exception:
            pass
            
        reply_kb = [
            [KeyboardButton("📍 Share Location", request_location=True)],
        ]
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📍 Please tap *Share Location* below to share your GPS location, or type your address manually:",
            reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return STATE_NEW_ADDRESS
    return ConversationHandler.END


async def msg_profile_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    name = update.effective_message.text.strip()
    
    # Try deleting user incoming name message
    try:
        await update.effective_message.delete()
    except Exception:
        pass
        
    try:
        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        profile.display_name = name
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
            user_id=user_id,
        )
        
        # Check if onboarding
        if context.user_data.get("registering"):
            # Update the old bot message inline to ask for phone
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data.get("last_bot_msg_id"),
                text=f"✅ Name saved: *{name}*\n\n📱 *Step 2:* Please enter your phone number:",
                parse_mode=ParseMode.MARKDOWN
            )
            return STATE_PROFILE_PHONE
            
        # Standard Profile Edit
        # Delete old bot message and send a clean updated profile
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
        except Exception:
            pass
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ Name updated to *{name}*"
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        await cmd_profile(update, context)
    except Exception as e:
        await _safe_reply(update, f"⚠️ Could not update profile: {e}")
    return ConversationHandler.END


async def msg_profile_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    phone = update.effective_message.text.strip()
    
    # Try deleting user incoming phone message
    try:
        await update.effective_message.delete()
    except Exception:
        pass
        
    try:
        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        profile.phone = phone
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
            user_id=user_id,
        )
        
        # Check if onboarding
        if context.user_data.get("registering"):
            # Update the old bot message and prompt for address with location keyboard
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
            except Exception:
                pass
                
            reply_kb = [
                [KeyboardButton("📍 Share Location", request_location=True)],
            ]
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"✅ Phone saved: *{phone}*\n\n"
                    "📍 *Step 3:* Awesome! Almost done.\n\n"
                    "An address is required to calculate delivery distances.\n\n"
                    "👉 Please tap *Share Location* below to share your GPS location, or type your address manually:"
                ),
                reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data["last_bot_msg_id"] = msg.message_id
            return STATE_NEW_ADDRESS
            
        # Standard Profile Edit
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
        except Exception:
            pass
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Phone number updated"
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        await cmd_profile(update, context)
    except Exception as e:
        await _safe_reply(update, f"⚠️ Could not update profile: {e}")
    return ConversationHandler.END


@rate_limit
async def cmd_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Try deleting incoming message
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    # Try deleting previous bot message
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
    except Exception:
        pass

    user_id = update.effective_user.id
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Could not load addresses."
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return

    lines = ["📍 *Saved Addresses*\n"]
    kb = []
    if resp.addresses:
        for a in resp.addresses:
            label = ("⭐ " if a.is_default else "") + a.label
            lines.append(f"• {label}: {a.address}")
            row = [
                InlineKeyboardButton(f"🏠 Default: {a.label}", callback_data=f"addr_default:{a.id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"addr_delete:{a.id}"),
            ]
            kb.append(row)
    else:
        lines.append("No saved addresses.")

    kb.append([InlineKeyboardButton("➕ Add Address", callback_data="profile_edit:add_address")])
    kb.append([InlineKeyboardButton("🔙 Back to Profile", callback_data="profile_edit:view")])

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["last_bot_msg_id"] = msg.message_id


async def _edit_address_list(query, user_id: int) -> None:
    # Universal edit helper
    async def edit_msg(text, **kwargs):
        chat_id = query.message.chat_id if hasattr(query, "message") and query.message is not None else query.chat_id
        message_id = query.message.message_id if hasattr(query, "message") and query.message is not None else query.message_id
        return await query.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        await edit_msg("⚠️ Could not load addresses.")
        return

    lines = ["📍 *Saved Addresses*\n"]
    kb = []
    if resp.addresses:
        for a in resp.addresses:
            label = ("⭐ " if a.is_default else "") + a.label
            lines.append(f"• {label}: {a.address}")
            row = [
                InlineKeyboardButton(f"🏠 Default: {a.label}", callback_data=f"addr_default:{a.id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"addr_delete:{a.id}"),
            ]
            kb.append(row)
    else:
        lines.append("No saved addresses.")

    kb.append([InlineKeyboardButton("➕ Add Address", callback_data="profile_edit:add_address")])
    kb.append([InlineKeyboardButton("🔙 Back to Profile", callback_data="profile_edit:view")])

    await edit_msg(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )


async def cb_address_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    addr_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        addr = next((a for a in resp.addresses if a.id == addr_id), None)
        if addr:
            addr.is_default = True
            await pool.call(
                "/logistics.LogisticsEngine/SaveAddress",
                lambda stub, meta: stub.SaveAddress(addr, metadata=meta),
                user_id=user_id,
            )
            await query.answer("✅ Default address updated", show_alert=True)
            await _edit_address_list(query, user_id)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)


async def cb_address_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    addr_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id
    try:
        await pool.call(
            "/logistics.LogisticsEngine/DeleteAddress",
            lambda stub, meta: stub.DeleteAddress(
                pb.AddressId(id=addr_id), metadata=meta
            ),
        )
        await query.answer("🗑 Address deleted", show_alert=True)
        await _edit_address_list(query, user_id)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)


@rate_limit
async def cmd_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        await update.effective_message.delete()
    except Exception:
        pass
        
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
    except Exception:
        pass
        
    reply_kb = [
        [KeyboardButton("📍 Share Location", request_location=True)],
    ]
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📍 Please tap *Share Location* below to share your GPS location, or type your address manually:",
        reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data["last_bot_msg_id"] = msg.message_id
    return STATE_NEW_ADDRESS


async def msg_new_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    msg = update.effective_message
    
    # Try deleting user incoming message
    try:
        await msg.delete()
    except Exception:
        pass
        
    lat = 0.0
    lon = 0.0
    if msg.location:
        lat = msg.location.latitude
        lon = msg.location.longitude
        address_text = f"GPS: ({lat:.6f}, {lon:.6f})"
    else:
        address_text = msg.text.strip()
        
    try:
        await pool.call(
            "/logistics.LogisticsEngine/SaveAddress",
            lambda stub, meta: stub.SaveAddress(
                pb.SavedAddress(
                    user_id=user_id,
                    label="Home",
                    address=address_text,
                    lat=lat,
                    lon=lon,
                    is_default=True,
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )
        
        # Remove old bot message
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
        except Exception:
            pass
            
        # Clean reply keyboard cleanly
        try:
            rm_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🧹 Cleaning keyboard...",
                reply_markup=ReplyKeyboardRemove(),
            )
            await rm_msg.delete()
        except Exception:
            pass

        if context.user_data.get("registering"):
            context.user_data.pop("registering", None)
            
            kb = [
                [InlineKeyboardButton("🛒 Browse Catalog", callback_data="cat_select:all:0")],
                [InlineKeyboardButton("🛍 My Cart",   callback_data="checkout"),
                 InlineKeyboardButton("📋 My Orders", callback_data="order_detail:list")],
                [InlineKeyboardButton("👤 Profile",   callback_data="profile_edit:view"),
                 InlineKeyboardButton("🎁 Points",    callback_data="loyalty_view")],
                [InlineKeyboardButton("❓ Help",       callback_data="help")],
            ]
            bot_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🎉 *Account Setup Complete!*\n\nWelcome to Logistics Engine! Your profile and default address are now configured. You can start shopping immediately:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data["last_bot_msg_id"] = bot_msg.message_id
        else:
            # Standard address add success
            bot_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✅ Address saved successfully!",
            )
            # Fetch address list and edit the screen inline
            await _edit_address_list(bot_msg, user_id)
            
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ {e}",
            reply_markup=ReplyKeyboardRemove()
        )
    return ConversationHandler.END


# ── Loyalty & promos ──────────────────────────────────────────────────────────

@rate_limit
async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        bal = await pool.call(
            "/logistics.LogisticsEngine/GetLoyaltyBalance",
            lambda stub, meta: stub.GetLoyaltyBalance(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        await _safe_reply(
            update,
            f"🏅 *Loyalty Points*\n\n"
            f"Balance: *{bal.points} points*\n"
            f"Tier: {bal.tier.title()}\n\n"
            f"Earn more points by placing orders!",
        )
    except Exception as e:
        await _safe_reply(update, f"⚠️ {e}")


@rate_limit
async def cb_loyalty_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    try:
        bal = await pool.call(
            "/logistics.LogisticsEngine/GetLoyaltyBalance",
            lambda stub, meta: stub.GetLoyaltyBalance(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        await query.edit_message_text(
            f"🏅 *Loyalty Points*\n\n"
            f"Balance: *{bal.points} points*\n"
            f"Tier: {bal.tier.title()}\n\n"
            f"Earn more points by placing orders!",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await query.edit_message_text(f"⚠️ Could not load points: {e}")


@rate_limit
async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-041: Referral code."""
    user_id = update.effective_user.id
    code = f"REF{user_id}"
    await _safe_reply(
        update,
        f"🎁 *Refer a Friend*\n\n"
        f"Share your code: `{code}`\n\n"
        f"When a friend places their first order using your code, "
        f"you both earn 100 bonus loyalty points!",
    )


# ── Spending, notifications, language, GDPR ──────────────────────────────────

@rate_limit
async def cmd_spending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-032: Monthly spending summary."""
    user_id = update.effective_user.id
    now = int(time.time())
    month_start = now - (now % (30 * 86400))

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetUserOrders",
            lambda stub, meta: stub.GetUserOrders(
                pb.GetUserOrdersRequest(user_id=user_id, page=0, page_size=100),
                metadata=meta,
            ),
            user_id=user_id,
        )
        total = sum(o.total for o in resp.orders if o.timestamp >= month_start)
        count = sum(1 for o in resp.orders if o.timestamp >= month_start)
        await _safe_reply(
            update,
            f"💰 *This Month's Spending*\n\n"
            f"Orders: {count}\n"
            f"Total spent: LKR {total:.0f}\n"
            f"Average order: LKR {total / count:.0f}" if count else "No orders this month.",
        )
    except Exception as e:
        await _safe_reply(update, f"⚠️ {e}")


@rate_limit
async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-048: Notification preference toggle."""
    user_id = update.effective_user.id
    try:
        prefs = await pool.call(
            "/logistics.LogisticsEngine/GetNotifPrefs",
            lambda stub, meta: stub.GetNotifPrefs(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        prefs = pb.NotifPrefs(user_id=user_id, order_updates=True, system_alerts=True)

    def toggle_btn(label: str, field: str, current: bool) -> InlineKeyboardButton:
        icon = "✅" if current else "❌"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"notif_toggle:{field}")

    kb = [
        [toggle_btn("Order Updates",  "order_updates",  prefs.order_updates)],
        [toggle_btn("Promotions",     "promotions",     prefs.promotions)],
        [toggle_btn("New Products",   "new_products",   prefs.new_products)],
        [toggle_btn("Flash Deals",    "flash_deals",    prefs.flash_deals)],
        [toggle_btn("System Alerts",  "system_alerts",  prefs.system_alerts)],
    ]
    await _safe_reply(update, "🔔 *Notification Preferences*\nTap to toggle:",
                      reply_markup=InlineKeyboardMarkup(kb))


async def cb_notif_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        prefs = await pool.call(
            "/logistics.LogisticsEngine/GetNotifPrefs",
            lambda stub, meta: stub.GetNotifPrefs(pb.UserId(user_id=user_id), metadata=meta),
            user_id=user_id,
        )
        # Toggle the field
        current = getattr(prefs, field, False)
        setattr(prefs, field, not current)
        await pool.call(
            "/logistics.LogisticsEngine/SetNotifPrefs",
            lambda stub, meta: stub.SetNotifPrefs(prefs, metadata=meta),
            user_id=user_id,
        )
        await query.answer("Preference updated!", show_alert=False)
        # Refresh the message
        await cmd_notifications(update, context)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)


@rate_limit
async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-049: Language selection."""
    kb = [
        [InlineKeyboardButton("🇬🇧 English",  callback_data="lang_select:en")],
        [InlineKeyboardButton("🇱🇰 සිංහල",  callback_data="lang_select:si")],
        [InlineKeyboardButton("🇱🇰 தமிழ்",   callback_data="lang_select:ta")],
    ]
    await _safe_reply(update, "🌐 *Select your language:*",
                      reply_markup=InlineKeyboardMarkup(kb))


async def cb_language_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        profile.language = lang
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
            user_id=user_id,
        )
        await query.edit_message_text(f"✅ Language set to *{lang}*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await query.answer(f"⚠️ Could not set language: {e}", show_alert=True)


@rate_limit
async def cmd_delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """U-036: GDPR data deletion request."""
    kb = [
        [InlineKeyboardButton("⚠️ Yes, delete my data", callback_data="confirm_delete")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ]
    await _safe_reply(
        update,
        "⚠️ *Delete Account*\n\n"
        "This will anonymise all your personal data (name, phone, addresses).\n"
        "Your order history will be retained for legal/accounting purposes but "
        "will no longer be linked to your identity.\n\n"
        "Are you sure?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ConversationHandler.END


async def cb_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    try:
        # Anonymise profile
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(
                pb.CustomerProfile(
                    user_id=user_id,
                    display_name="[deleted]",
                    phone="",
                    language="en",
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )
        # Clear saved addresses
        addr_resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        for a in addr_resp.addresses:
            addr = a  # capture loop variable by value
            await pool.call(
                "/logistics.LogisticsEngine/DeleteAddress",
                lambda stub, meta, _addr=addr: stub.DeleteAddress(
                    pb.AddressId(id=_addr.id), metadata=meta
                ),
            )

        await query.edit_message_text(
            "✅ Your personal data has been removed. Thank you for using our service."
        )
    except Exception as e:
        await query.edit_message_text(f"⚠️ Could not delete account: {e}")

    return ConversationHandler.END


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Global query callback for cancelling an operation."""
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("❌ Operation cancelled.")
    else:
        await _safe_reply(update, "❌ Operation cancelled.")
    return ConversationHandler.END


@rate_limit
async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-046: Live support command."""
    await _safe_reply(
        update,
        "🆘 *Support*\n\n"
        "Our support team will be with you shortly.\n\n"
        "Please describe your issue and include your order number if applicable "
        "(e.g. ORD-20260513-0047).\n\n"
        "You can also use /report to report an issue with a specific order.",
    )


# ── Notification polling ───────────────────────────────────────────────────────

async def poll_notifications(app) -> None:
    """
    Background task: poll the engine for pending notifications every 30 s
    and forward them to users via Telegram.
    """
    from telegram.error import TelegramError

    # We need all unique user IDs that have interacted — stored in bot_data
    while True:
        await asyncio.sleep(30)
        try:
            # Fetch all admin IDs to notify them too
            admin_resp = await pool.call(
                "/logistics.LogisticsEngine/ListAdmins",
                lambda stub, meta: stub.ListAdmins(pb.Empty(), metadata=meta),
            )
            user_ids = [a.telegram_id for a in admin_resp.admins]

            for uid in user_ids:
                try:
                    notif_resp = await pool.call(
                        "/logistics.LogisticsEngine/GetPendingNotifs",
                        lambda stub, meta, _uid=uid: stub.GetPendingNotifs(
                            pb.UserId(user_id=_uid), metadata=meta
                        ),
                        user_id=uid,
                    )
                    for notif in notif_resp.notifs:
                        try:
                            await app.bot.send_message(
                                chat_id=uid,
                                text=notif.text,
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except TelegramError:
                            pass
                except Exception:
                    pass
        except Exception:
            pass


async def cb_orders_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    context.user_data["orders_page"] = page
    await cmd_orders(update, context)


async def cb_orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["orders_page"] = 0
    await cmd_orders(update, context)


async def cb_issue_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    order_id = parts[1]
    issue_type = parts[2]
    try:
        await pool.call(
            "/logistics.LogisticsEngine/UpdateOrderStatus",
            lambda stub, meta: stub.UpdateOrderStatus(
                pb.UpdateOrderStatusRequest(
                    order_id=order_id,
                    status="issue",
                    reason=f"Customer reported issue: {issue_type}"
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await query.edit_message_text(f"✅ Issue reported ({issue_type}). Our support team will contact you shortly.")
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ {e.details()}")


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fix welcome screen Help button spinner."""
    query = update.callback_query
    await query.answer()
    text = (
        "❓ *Help & FAQ*\n\n"
        "*How to order*\n"
        "Use /catalog to browse items, add them to your cart, then /cart to checkout.\n\n"
        "*Track my delivery*\n"
        "Use /orders to see your current order status and ETA updates.\n\n"
        "*Cancel an order*\n"
        "Use /orders → tap the order → Cancel (within 5 minutes of placing).\n\n"
        "*Contact support*\n"
        "Use /support to open a live support chat with our team.\n\n"
        "*Promo codes*\n"
        "Enter your promo code at checkout when prompted.\n\n"
        "*Loyalty points*\n"
        "Earn points on every order. Check your balance with /points."
    )
    await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

