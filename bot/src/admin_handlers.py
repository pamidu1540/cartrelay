"""
Admin panel Telegram handlers.

Key Features & Fixes:
  - Phase 0: UpdateOrderStatus response carries notify_user_id as a proto field.
  - Phase 0: Order list is paginated via the new GetAllOrders RPC.
  - Phase 1: Warehouse Management (Add/List) via native Telegram location pins.
  - Security: Admin ID check extracted to a single helper used everywhere.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from api import logistics_pb2
from grpc_client import engine

logger = logging.getLogger(__name__)

# ── Admin access control ──────────────────────────────────────────────────────

_ADMIN_IDS: set[int] = set()
_raw = os.getenv("ADMIN_USER_IDS", "")
for _part in _raw.split(","):
    _part = _part.strip()
    if _part.isdigit():
        _ADMIN_IDS.add(int(_part))


def is_admin(user_id: int) -> bool:
    return user_id in _ADMIN_IDS


async def _require_admin(update: Update) -> bool:
    """Reply with a rejection message if user is not an admin. Returns True if admin."""
    if is_admin(update.effective_user.id):
        return True
    text = "⛔ You are not authorised to use the admin panel."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    else:
        await update.message.reply_text(text)
    return False


# ── Conversation states ───────────────────────────────────────────────────────
(
    ADMIN_ADD_NAME,
    ADMIN_ADD_PRICE,
    ADMIN_ADD_VOL,
    ADMIN_ADD_CAT,
    ADMIN_ADD_STOCK,
    ADMIN_EDIT_STOCK_ITEM,
    ADMIN_EDIT_STOCK_DELTA,
    ADMIN_WH_NAME,
    ADMIN_WH_LOC,
) = range(9)


# ── Dashboard ─────────────────────────────────────────────────────────────────

async def admin_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    try:
        health = await engine.check_health()
        maint  = "🔴 ON" if health.maintenance_mode else "🟢 OFF"
        ver    = health.version
    except Exception as exc:
        logger.error("admin health check: %s", exc)
        maint, ver = "❓ unknown", "❓"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 View Catalog",         callback_data="admin_view_cat")],
        [InlineKeyboardButton("📋 Pending Orders",       callback_data="admin_view_orders_pending")],
        [InlineKeyboardButton("📋 All Orders",           callback_data="admin_view_orders_all")],
        [InlineKeyboardButton("➕ Add Item",             callback_data="admin_add_item")],
        [InlineKeyboardButton("📈 Adjust Stock",         callback_data="admin_edit_stock")],
        [
            InlineKeyboardButton("➕ Add Warehouse", callback_data="admin_add_wh"),
            InlineKeyboardButton("🏢 View Warehouses", callback_data="admin_list_wh")
        ],
        [InlineKeyboardButton(f"🔧 Maintenance: {maint}", callback_data="admin_toggle_maint")],
    ])

    text = (
        f"⚙️ *Admin Dashboard*\n"
        f"Engine: v{ver}  |  Maintenance: {maint}"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )


# ── Catalog view ──────────────────────────────────────────────────────────────

async def admin_view_catalog(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return

    try:
        catalog = await engine.get_catalog()
    except Exception as exc:
        await query.edit_message_text(f"⚠️ Catalog error: {exc}")
        return

    if not catalog.items:
        await query.edit_message_text("Catalog is empty.")
        return

    lines = []
    for item in sorted(catalog.items.values(), key=lambda x: (x.category, x.name)):
        lines.append(
            f"• [{item.id}] *{item.name}*\n"
            f"  LKR {item.price:.2f}  |  Stock: {item.stock}  |  {item.category}"
        )

    await query.edit_message_text(
        "📦 *Catalog*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ]),
    )


# ── Orders ────────────────────────────────────────────────────────────────────

async def _show_orders(
    update: Update,
    status_filter: str = "pending",
    page: int = 0,
) -> None:
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return

    try:
        resp = await engine.get_all_orders(
            status_filter=status_filter, page=page, page_size=10
        )
    except Exception as exc:
        await query.edit_message_text(f"⚠️ Error fetching orders: {exc}")
        return

    orders = resp.orders
    total  = resp.total_count

    if not orders:
        await query.edit_message_text(
            f"No {'pending ' if status_filter == 'pending' else ''}orders found.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
            ]),
        )
        return

    lines = []
    action_rows = []
    for o in orders:
        dt = datetime.fromtimestamp(o.timestamp, tz=timezone.utc).strftime("%d %b %H:%M")
        lines.append(
            f"`{o.order_id[:12]}…`  {o.status.upper()}\n"
            f"  LKR {o.total:.2f}  {dt}"
        )
        if o.status == "pending":
            action_rows.append([
                InlineKeyboardButton(f"✅ Dispatch {o.order_id[:8]}", callback_data=f"dispatch_{o.order_id}"),
                InlineKeyboardButton("❌ Cancel",                    callback_data=f"cancelord_{o.order_id}"),
            ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"orders_page_{status_filter}_{page-1}"))
    if (page + 1) * 10 < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"orders_page_{status_filter}_{page+1}"))

    rows = action_rows
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")])

    await query.edit_message_text(
        f"📋 *Orders* (pg {page+1}, {total} total)\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_view_orders_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_orders(update, status_filter="pending")


async def admin_view_orders_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_orders(update, status_filter="")


async def admin_orders_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split("_")
    page   = int(parts[-1])
    status = parts[-2] if parts[-2] != "orders" else ""
    await _show_orders(update, status_filter=status, page=page)


async def admin_dispatch_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return

    order_id = query.data.removeprefix("dispatch_")

    try:
        resp = await engine.update_order_status(order_id, "dispatched")
    except Exception as exc:
        await query.edit_message_text(f"⚠️ Dispatch failed: {exc}")
        return

    if resp.notify_user_id:
        try:
            await ctx.bot.send_message(
                chat_id=resp.notify_user_id,
                text=(
                    f"🚚 Your order `{order_id[:12]}…` has been dispatched!\n"
                    "You should receive it soon."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.warning("push notification failed user=%d: %s", resp.notify_user_id, exc)

    await query.edit_message_text(
        f"✅ Order `{order_id[:12]}…` dispatched.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Orders", callback_data="admin_view_orders_pending")]
        ]),
    )


async def admin_cancel_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return

    order_id = query.data.removeprefix("cancelord_")

    try:
        resp = await engine.update_order_status(order_id, "cancelled")
    except Exception as exc:
        await query.edit_message_text(f"⚠️ Cancel failed: {exc}")
        return

    if resp.notify_user_id:
        try:
            await ctx.bot.send_message(
                chat_id=resp.notify_user_id,
                text=f"❌ Your order `{order_id[:12]}…` has been cancelled.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.warning("push notification failed user=%d: %s", resp.notify_user_id, exc)

    await query.edit_message_text(
        f"Order `{order_id[:12]}…` cancelled.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Orders", callback_data="admin_view_orders_pending")]
        ]),
    )


# ── Maintenance toggle ────────────────────────────────────────────────────────

async def admin_toggle_maintenance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return

    try:
        resp = await engine.toggle_maintenance()
    except Exception as exc:
        await query.edit_message_text(f"⚠️ Toggle failed: {exc}")
        return

    await query.edit_message_text(
        f"🔧 {resp.message}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ]),
    )


# ── Warehouses ────────────────────────────────────────────────────────────────

async def admin_list_warehouses(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetches all warehouses and sends them to the admin as Telegram location pins."""
    query = update.callback_query
    await query.answer()
    if not await _require_admin(update):
        return

    try:
        resp = await engine.list_warehouses()
    except Exception as exc:
        await query.edit_message_text(f"⚠️ Failed to list warehouses: {exc}")
        return

    if not resp.warehouses:
        await query.edit_message_text(
            "No warehouses configured yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
            ])
        )
        return

    await query.edit_message_text("📍 Here are the active warehouses:")
    for wh in resp.warehouses:
        await ctx.bot.send_location(
            chat_id=update.effective_chat.id,
            latitude=wh.lat,
            longitude=wh.lon,
        )
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🏢 *{wh.name}*\nID: `{wh.id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Warehouse listing complete.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ])
    )


async def wizard_start_add_warehouse(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Not authorised.", show_alert=True)
        return ConversationHandler.END

    ctx.user_data.clear()
    await query.edit_message_text(
        "🏢 *New Warehouse*\n\nEnter a name for the new warehouse (e.g., 'South Depot'):",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADMIN_WH_NAME


async def wizard_receive_wh_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["wh_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"Great. Now use Telegram's attachment menu (📎) to send the exact Location of '{ctx.user_data['wh_name']}'.\n"
        "(Or type /cancel to abort)"
    )
    return ADMIN_WH_LOC


async def wizard_receive_wh_loc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    if not loc:
        await update.message.reply_text("❌ Please send a valid Telegram Location attachment (📎).")
        return ADMIN_WH_LOC
        
    name = ctx.user_data.get("wh_name", "Unknown Warehouse")

    try:
        resp = await engine.add_warehouse(name, loc.latitude, loc.longitude)
        if resp.success:
            text = f"✅ {resp.message}"
        else:
            text = f"❌ Failed: {resp.message}"
    except Exception as exc:
        text = f"⚠️ Error: {exc}"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ])
    )
    ctx.user_data.clear()
    return ConversationHandler.END


# ── Add item wizard ───────────────────────────────────────────────────────────

async def wizard_start_add_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Not authorised.", show_alert=True)
        return ConversationHandler.END

    ctx.user_data.clear()
    await query.edit_message_text("➕ *New Item*\n\nEnter item *name*:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_ADD_NAME


async def wizard_receive_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["item_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"Name: *{ctx.user_data['item_name']}*\n\nEnter *price* (LKR):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_ADD_PRICE


async def wizard_receive_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ctx.user_data["item_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Enter a number (e.g. 1500):")
        return ADMIN_ADD_PRICE
    await update.message.reply_text("Enter *volume* in m³ (e.g. 0.5):", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_ADD_VOL


async def wizard_receive_vol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        ctx.user_data["item_vol"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid volume. Enter a number (e.g. 0.5):")
        return ADMIN_ADD_VOL
    await update.message.reply_text("Enter *category* (e.g. Electronics):", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_ADD_CAT


async def wizard_receive_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["item_cat"] = update.message.text.strip()
    await update.message.reply_text("Enter initial *stock* quantity:", parse_mode=ParseMode.MARKDOWN)
    return ADMIN_ADD_STOCK


async def wizard_receive_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        stock = int(update.message.text.strip())
        if stock < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Stock must be a non-negative integer:")
        return ADMIN_ADD_STOCK

    item = logistics_pb2.Item(
        name         = ctx.user_data["item_name"],
        price        = ctx.user_data["item_price"],
        volume_cubes = ctx.user_data["item_vol"],
        category     = ctx.user_data["item_cat"],
        stock        = stock,
        photo_file_id= "",
    )
    try:
        resp = await engine.add_item(item)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Failed to add item: {exc}")
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ {resp.message}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ]),
    )
    ctx.user_data.clear()
    return ConversationHandler.END


# ── Adjust stock wizard ───────────────────────────────────────────────────────

async def wizard_start_edit_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Not authorised.", show_alert=True)
        return ConversationHandler.END

    ctx.user_data.clear()
    await query.edit_message_text(
        "📈 *Adjust Stock*\n\nEnter the *item ID* to adjust:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_EDIT_STOCK_ITEM


async def wizard_receive_stock_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["stock_item_id"] = update.message.text.strip()
    await update.message.reply_text(
        f"Item: `{ctx.user_data['stock_item_id']}`\n\n"
        "Enter *delta* (+N to restock, −N to deduct):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_EDIT_STOCK_DELTA


async def wizard_receive_stock_delta(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        delta = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter an integer (e.g. +50 or -5):")
        return ADMIN_EDIT_STOCK_DELTA

    item_id = ctx.user_data.get("stock_item_id", "")
    try:
        resp = await engine.edit_item_stock(item_id, delta)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Failed: {exc}")
        return ConversationHandler.END

    await update.message.reply_text(
        f"{'✅' if resp.success else '❌'} {resp.message}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ]),
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel_wizard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "Wizard cancelled.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dash")]
        ]),
    )
    return ConversationHandler.END