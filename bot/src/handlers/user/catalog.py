"""
catalog.py — Customer catalog browsing, category selection, item detail, and restock notifications.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

import grpc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from message_manager import send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import check_maintenance, rate_limit
from handlers.user.common import PAGE_SIZE, _cart

logger = logging.getLogger(__name__)


# ── Catalog Browsing ──────────────────────────────────────────────────────────

@rate_limit
@check_maintenance
async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-011: Show category buttons for browsing."""
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=context.user_data.get("last_bot_msg_id", 0),
        )
    except Exception:
        pass

    disclaimer = ""

    async def _fetch_wh():
        try:
            r = await pool.call(
                "/logistics.LogisticsEngine/ListWarehouses",
                lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta),
            )
            return len(r.warehouses) > 0
        except Exception:
            return False

    async def _fetch_cat():
        now = time.time()
        if now - getattr(context.bot_data, "_cat_time", 0.0) < 300:
            return context.bot_data["_cat_list"]
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(pb.CatalogRequest(page=0, page_size=200), metadata=meta),
        )
        cats = sorted({it.category for it in resp.items if it.is_available})
        context.bot_data["_cat_time"] = now
        context.bot_data["_cat_list"] = cats
        return cats

    try:
        wh_task = asyncio.create_task(_fetch_wh())
        cat_task = asyncio.create_task(_fetch_cat())
        has_wh = await wh_task
        categories = await cat_task
        has_cat = len(categories) > 0
    except grpc.RpcError as e:
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Could not load catalog: {e.details()}",
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return

    if not has_wh or not has_cat:
        disclaimer = (
            "⚠️ *Disclaimer:* The system is currently in initial setup mode. "
            "Admin options (such as warehouses or catalog items) have not been fully configured/enabled yet. "
            "Some features may be limited.\n\n"
        )

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
    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        query = await query.bot.send_message(
            chat_id=query.message.chat_id,
            text="⌛ Loading catalog...",
        )

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
    user_id = update.effective_user.id

    async def _get_item():
        return await pool.call(
            "/logistics.LogisticsEngine/GetItem",
            lambda stub, meta: stub.GetItem(pb.ItemId(id=item_id), metadata=meta),
        )

    async def _get_addr():
        try:
            return await pool.call(
                "/logistics.LogisticsEngine/ListSavedAddresses",
                lambda stub, meta: stub.ListSavedAddresses(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            return None

    async def _get_wh():
        try:
            return await pool.call(
                "/logistics.LogisticsEngine/ListWarehouses",
                lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta),
            )
        except Exception:
            return None

    try:
        item, addr_resp, wh_resp = await asyncio.gather(_get_item(), _get_addr(), _get_wh())
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")
        return

    stock_text = (
        "✅ In Stock" if item.stock > 5
        else f"⚠️ Only {item.stock} left!" if item.stock > 0
        else "❌ Out of Stock"
    )

    distance_info = ""
    if "dist_info" in context.user_data:
        distance_info = context.user_data["dist_info"]
    else:
        try:
            default_addr = next((a for a in addr_resp.addresses if a.is_default), None) if addr_resp else None
            active_warehouses = [w for w in wh_resp.warehouses if w.is_active] if wh_resp else []

            if not default_addr:
                distance_info = "📍 _Set up address in Profile to see delivery distance._"
            elif default_addr.lat == 0.0 or default_addr.lon == 0.0:
                distance_info = "📍 _Distance unavailable. Share location in Profile for exact distance._"
            elif not active_warehouses:
                distance_info = "📍 _No active warehouses found for delivery calculation._"
            else:
                min_dist = float("inf")
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

        context.user_data["dist_info"] = distance_info

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
        kb.append([InlineKeyboardButton(
            "🔔 Notify me when available",
            callback_data=f"stock_notify:{item_id}",
        )])

    kb.append([
        InlineKeyboardButton("⬅ Back to Catalog", callback_data=f"cat_select:{item.category or 'all'}:0"),
        InlineKeyboardButton("🏠 Home", callback_data="home"),
    ])

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
    parts = query.data.split(":")
    item_id = parts[1]
    qty_str = parts[2] if len(parts) > 2 else "1"
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
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ],
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


async def _show_catalog_categories_helper(query) -> None:
    try:
        now = time.time()
        if now - getattr(query.bot.data, "_cat_time", 0.0) < 300:
            categories = query.bot.data["_cat_list"]
        else:
            resp = await pool.call(
                "/logistics.LogisticsEngine/GetCatalog",
                lambda stub, meta: stub.GetCatalog(
                    pb.CatalogRequest(page=0, page_size=200), metadata=meta
                ),
            )
            categories = sorted({it.category for it in resp.items if it.is_available})
            query.bot.data["_cat_time"] = now
            query.bot.data["_cat_list"] = categories
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ Could not load catalog: {e.details()}")
        return

    kb = [[InlineKeyboardButton(f"📂 {cat}", callback_data=f"cat_select:{cat}:0")]
          for cat in categories]
    kb.append([InlineKeyboardButton("🔍 All Items", callback_data="cat_select:all:0")])
    kb.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="profile_edit:back_to_main")])
    await query.edit_message_text(
        "🛒 *Browse our catalog*\nChoose a category:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )
