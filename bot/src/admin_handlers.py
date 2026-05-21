"""
admin_handlers.py — all admin-facing Telegram handlers (v5)

Phase 1 admin features implemented:
  A-001: Warehouse CRUD (/warehouses, /addwh, /removewh)
  A-007: Bulk item import via CSV (/bulkimport)
  A-016: Order management dashboard (/allorders, /ordersearch)
  A-017: Order timeline view (status history in order detail)
  A-018: Manual order status override with mandatory reason
  A-019: Assign / reassign driver from order
  A-020: Internal order notes (/addnote)
  A-026: Driver profile management (/drivers, /adddriver, /removedriver)
  A-030: Driver broadcast (/broadcast)
  A-034: Promo code creator (/addpromo, /removepromo)
  A-035: Active promotion manager (/promos)
  A-039: Loyalty points adjustment (/adjustloyalty)
  A-041: Low-stock dashboard (/lowstock)
  A-047: Revenue dashboard (/revenue)
  A-048: Order volume trend (/ordertrend)
  A-049: Top-selling items (/topitems)
  A-055: Admin user management (/admins, /addadmin, /removeadmin)
  A-056: Audit log viewer (/audit)
  A-057: System settings panel (/settings)
  A-059: Maintenance mode scheduler (/maint)
"""

from __future__ import annotations

import csv
import io
import logging
import time

import grpc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import admin_only

logger = logging.getLogger(__name__)

# ConversationHandler states — must match main.py definition order
(
    STATE_ADDRESS, STATE_PROMO, STATE_SLOT, STATE_CONFIRM,
    STATE_ITEM_NAME, STATE_ITEM_PRICE, STATE_ITEM_STOCK, STATE_ITEM_CATEGORY,
    STATE_ITEM_PHOTO, STATE_ITEM_DESC, STATE_STATUS_REASON, STATE_NOTE_TEXT,
    STATE_ORDER_SEARCH, STATE_WH_NAME, STATE_WH_COORDS, STATE_DRV_NAME,
    STATE_DRV_PHONE, STATE_PROMO_DEF, STATE_BROADCAST, STATE_LOYALTY_ADJ,
    STATE_NEW_ADMIN, STATE_SETTINGS_VAL, STATE_MAINT_WINDOW, STATE_RESOLVE_NOTE,
    STATE_PROFILE_NAME, STATE_PROFILE_PHONE, STATE_NEW_ADDRESS, STATE_PROMO_CODE,
    STATE_CSV_UPLOAD,
) = range(29)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _reply(update: Update, text: str, **kwargs) -> None:
    msg = update.effective_message
    if not msg:
        return
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)


async def _rpc_error(update: Update, exc: grpc.RpcError) -> None:
    await _reply(update, f"⚠️ Engine error: {exc.details()}")


# ── Main admin panel ──────────────────────────────────────────────────────────

@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = [
        [InlineKeyboardButton("📦 Catalog",    callback_data="admin_menu:catalog"),
         InlineKeyboardButton("📋 Orders",     callback_data="admin_menu:orders")],
        [InlineKeyboardButton("🏭 Warehouses", callback_data="admin_menu:warehouses"),
         InlineKeyboardButton("🚗 Drivers",    callback_data="admin_menu:drivers")],
        [InlineKeyboardButton("🏷 Promos",     callback_data="admin_menu:promos"),
         InlineKeyboardButton("📊 Analytics",  callback_data="admin_menu:analytics")],
        [InlineKeyboardButton("👥 Admins",     callback_data="admin_menu:admins"),
         InlineKeyboardButton("⚙️ Settings",   callback_data="admin_menu:settings")],
        [InlineKeyboardButton("🔒 Maintenance",callback_data="admin_menu:maintenance"),
         InlineKeyboardButton("📜 Audit Log",  callback_data="admin_menu:audit")],
    ]
    await _reply(update, "🔧 *Admin Panel*\nSelect a section:",
                 reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def cb_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    section = query.data.split(":", 1)[1]

    dispatch = {
        "orders":      cmd_all_orders,
        "warehouses":  cmd_warehouses,
        "drivers":     cmd_drivers,
        "promos":      cmd_promos,
        "admins":      cmd_admins,
        "settings":    cmd_settings,
        "maintenance": cmd_maintenance,
        "audit":       cmd_audit,
    }
    if section in dispatch:
        await dispatch[section](update, context)
    elif section == "catalog":
        kb = [
            [InlineKeyboardButton("➕ Add Item",    callback_data="admin_menu:additem")],
            [InlineKeyboardButton("📥 Bulk Import", callback_data="admin_menu:bulkimport")],
            [InlineKeyboardButton("📉 Low Stock",   callback_data="admin_menu:lowstock")],
        ]
        await query.edit_message_text("📦 *Catalog Management*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    elif section == "analytics":
        kb = [
            [InlineKeyboardButton("💰 Revenue",   callback_data="admin_menu:do_revenue")],
            [InlineKeyboardButton("📈 Trend",     callback_data="admin_menu:do_trend")],
            [InlineKeyboardButton("🏆 Top Items", callback_data="admin_menu:do_topitems")],
        ]
        await query.edit_message_text("📊 *Analytics*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    elif section == "do_revenue":
        await cmd_revenue(update, context)
    elif section == "do_trend":
        await cmd_order_trend(update, context)
    elif section == "do_topitems":
        await cmd_top_items(update, context)
    elif section == "lowstock":
        await cmd_low_stock(update, context)


# ── Catalog management ────────────────────────────────────────────────────────

@admin_only
async def cmd_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_item"] = {}
    await _reply(update, "➕ *Add New Item*\n\nStep 1/6 — Enter the item *name*:")
    return STATE_ITEM_NAME


async def msg_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault("new_item", {})["name"] = update.effective_message.text.strip()
    await _reply(update, "Step 2/6 — Enter the *price* (LKR, numbers only):")
    return STATE_ITEM_PRICE


async def msg_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.effective_message.text.strip())
        if price < 0:
            raise ValueError("negative price")
    except ValueError:
        await _reply(update, "❌ Enter a valid positive number:")
        return STATE_ITEM_PRICE
    context.user_data["new_item"]["price"] = price
    await _reply(update, "Step 3/6 — Enter initial *stock* quantity:")
    return STATE_ITEM_STOCK


async def msg_item_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        stock = int(update.effective_message.text.strip())
        if stock < 0:
            raise ValueError("negative stock")
    except ValueError:
        await _reply(update, "❌ Enter a non-negative integer:")
        return STATE_ITEM_STOCK
    context.user_data["new_item"]["stock"] = stock
    await _reply(update, "Step 4/6 — Enter the *category* (e.g. Beverages, Snacks):")
    return STATE_ITEM_CATEGORY


async def msg_item_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_item"]["category"] = update.effective_message.text.strip()
    await _reply(update, "Step 5/6 — Send the item *photo* (or type `skip`):")
    return STATE_ITEM_PHOTO


async def msg_item_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg.photo:
        context.user_data["new_item"]["photo_file_id"] = msg.photo[-1].file_id
    await _reply(update, "Step 6/6 — Enter the item *description* (or type `skip`):")
    return STATE_ITEM_DESC


async def msg_item_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if text.lower() != "skip":
        context.user_data["new_item"]["description"] = text

    d = context.user_data.pop("new_item", {})
    item = pb.Item(
        name=d.get("name", ""),
        price=d.get("price", 0.0),
        stock=d.get("stock", 0),
        category=d.get("category", "Uncategorized"),
        photo_file_id=d.get("photo_file_id", ""),
        description=d.get("description", ""),
        is_available=True,
        tax_rate=-1,
    )
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddItem",
            lambda stub, meta: stub.AddItem(item, metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, f"✅ *{item.name}* added! ID: `{resp.entity_id}`")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


@admin_only
async def cmd_edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update, "✏️ Enter the item ID to edit (get IDs from /lowstock or /catalog):")
    return STATE_ITEM_NAME


@admin_only
async def cmd_remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, "Usage: `/removeitem <item_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveItem",
            lambda stub, meta: stub.RemoveItem(pb.ItemId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, "✅ Item removed." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, e)


@admin_only
async def cmd_bulk_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(
        update,
        "📥 *Bulk Import Items*\n\n"
        "Upload a CSV with columns:\n"
        "`name, price, stock, category, description`\n\n"
        "Send the CSV file now:",
    )
    return STATE_CSV_UPLOAD


async def msg_csv_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.effective_message.document
    if not doc:
        await _reply(update, "❌ Please send a CSV file.")
        return STATE_CSV_UPLOAD

    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")))

    items: list[pb.Item] = []
    for row in reader:
        try:
            items.append(pb.Item(
                name=row.get("name", "").strip(),
                price=float(row.get("price", 0)),
                stock=int(row.get("stock", 0)),
                category=row.get("category", "Uncategorized").strip(),
                description=row.get("description", "").strip(),
                is_available=True,
                tax_rate=-1,
            ))
        except (ValueError, KeyError) as exc:
            logger.warning("CSV row skip: %s", exc)

    if not items:
        await _reply(update, "❌ No valid rows found in CSV.")
        return ConversationHandler.END

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/BulkImportItems",
            lambda stub, meta: stub.BulkImportItems(
                pb.BulkImportRequest(items=items), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        msg = f"✅ Import done — created: {resp.created}, updated: {resp.updated}"
        if resp.errors:
            msg += f"\nErrors ({len(resp.errors)}):\n" + "\n".join(resp.errors[:5])
        await _reply(update, msg)
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


# ── Order management ──────────────────────────────────────────────────────────

@admin_only
async def cmd_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-016: Paginated admin order dashboard."""
    page = context.user_data.get("admin_orders_page", 0)
    status_f = context.user_data.get("admin_orders_status", "")

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetAllOrders",
            lambda stub, meta: stub.GetAllOrders(
                pb.GetAllOrdersRequest(status_filter=status_f, page=page, page_size=10),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
        return

    if not resp.orders:
        await _reply(update, "📋 No orders found.")
        return

    STATUS_EMOJI = {
        "pending": "🕐", "confirmed": "✅", "dispatched": "🚗",
        "delivered": "📦", "cancelled": "❌", "failed": "⚠️",
    }
    lines = [f"📋 *All Orders* (page {page + 1}/{max(1,(resp.total_count+9)//10)})\n"]
    kb = []
    for o in resp.orders:
        e = STATUS_EMOJI.get(o.status, "❓")
        lines.append(f"{e} *{o.human_ref}* @{o.username} — LKR {o.total:.0f}")
        kb.append([InlineKeyboardButton(
            f"{e} {o.human_ref} — {o.status}",
            callback_data=f"admin_order:{o.order_id}",
        )])

    # Status filter row
    kb.append([
        InlineKeyboardButton("All",        callback_data="admin_sf:"),
        InlineKeyboardButton("Pending",    callback_data="admin_sf:pending"),
        InlineKeyboardButton("Dispatched", callback_data="admin_sf:dispatched"),
        InlineKeyboardButton("Issues",     callback_data="admin_sf:issue"),
    ])

    # Pagination row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅", callback_data=f"admin_op:{page-1}"))
    if page < (resp.total_count - 1) // 10:
        nav.append(InlineKeyboardButton("➡", callback_data=f"admin_op:{page+1}"))
    if nav:
        kb.append(nav)

    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )


async def cb_admin_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-017: Order detail with full status timeline and action buttons."""
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]

    try:
        order = await pool.call(
            "/logistics.LogisticsEngine/GetOrder",
            lambda stub, meta: stub.GetOrder(pb.OrderId(order_id=order_id), metadata=meta),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ {e.details()}")
        return

    lines = [
        f"📋 *{order.human_ref}*",
        f"Status:   `{order.status}`",
        f"Customer: @{order.username} ({order.user_id})",
        f"Total:    LKR {order.total:.0f}",
        f"Address:  {order.delivery_address}",
    ]
    if order.driver_name:
        lines.append(f"Driver:   {order.driver_name}")
    if order.order_notes:
        lines.append(f"Notes:    _{order.order_notes}_")
    if order.issue_status not in ("", "none"):
        lines.append(f"⚠️ Issue: {order.issue_status}")

    # A-017: Status timeline
    if order.history:
        lines.append("\n📅 *Timeline:*")
        for h in order.history:
            ts = time.strftime("%H:%M %d %b", time.gmtime(h.ts))
            note = f"  _{h.note}_" if h.note else ""
            lines.append(f"  • `{ts}` {h.status}{note}")

    # A-020: Internal notes
    if order.notes:
        lines.append("\n📝 *Internal Notes:*")
        for n in order.notes:
            ts = time.strftime("%H:%M %d %b", time.gmtime(n.ts))
            lines.append(f"  • `{ts}` {n.text}")

    # Legal next-status transitions (mirrors engine state machine)
    TRANSITIONS: dict[str, list[str]] = {
        "pending":    ["confirmed", "cancelled"],
        "confirmed":  ["dispatched", "cancelled"],
        "dispatched": ["delivered", "failed"],
        "failed":     ["dispatched", "cancelled"],
    }
    kb = []
    for ns in TRANSITIONS.get(order.status, []):
        kb.append([InlineKeyboardButton(
            f"→ Mark {ns.title()}",
            callback_data=f"admin_status:{order_id}:{ns}",
        )])

    kb.append([
        InlineKeyboardButton("🚗 Assign Driver", callback_data=f"admin_assign:{order_id}"),
        InlineKeyboardButton("📝 Add Note",      callback_data=f"admin_note:{order_id}"),
    ])

    if order.issue_status == "open":
        kb.append([InlineKeyboardButton(
            "✅ Resolve Issue", callback_data=f"resolve_issue:{order_id}"
        )])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cb_admin_update_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A-018: Manual status override — mandatory reason before applying."""
    query = update.callback_query
    await query.answer()
    _, order_id, new_status = query.data.split(":", 2)
    context.user_data["pending_status_order"] = order_id
    context.user_data["pending_status_value"] = new_status
    await query.edit_message_text(
        f"📝 Moving order to *{new_status}*.\n\nEnter a mandatory reason:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return STATE_STATUS_REASON


async def msg_status_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id  = context.user_data.pop("pending_status_order", "")
    new_status = context.user_data.pop("pending_status_value", "")
    reason    = update.effective_message.text.strip()
    actor_id  = update.effective_user.id

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/UpdateOrderStatus",
            lambda stub, meta: stub.UpdateOrderStatus(
                pb.UpdateStatusRequest(
                    order_id=order_id,
                    new_status=new_status,
                    reason=reason,
                    actor_id=actor_id,
                ),
                metadata=meta,
            ),
            user_id=actor_id,
        )
        await _reply(
            update,
            f"✅ Order moved to *{new_status}*" if resp.success else f"❌ {resp.message}",
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


async def cb_admin_assign_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-019: Pick available driver for an order."""
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]
    context.user_data["assign_order_id"] = order_id

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListDrivers",
            lambda stub, meta: stub.ListDrivers(
                pb.ListDriversRequest(available_only=True), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await query.edit_message_text(f"⚠️ {e.details()}")
        return

    if not resp.drivers:
        await query.edit_message_text("⚠️ No available drivers right now.")
        return

    kb = [[InlineKeyboardButton(
        f"🚗 {d.name} ({d.vehicle_type})",
        callback_data=f"do_assign:{order_id}:{d.id}",
    )] for d in resp.drivers]

    await query.edit_message_text(
        "🚗 *Select a driver:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


@admin_only
async def cmd_order_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A-025: Search orders by customer or order ID."""
    await _reply(update, "🔍 Enter customer @username, name, or order number to search:")
    return STATE_ORDER_SEARCH


async def msg_order_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.effective_message.text.strip().lower()
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetAllOrders",
            lambda stub, meta: stub.GetAllOrders(
                pb.GetAllOrdersRequest(page=0, page_size=50), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        matches = [
            o for o in resp.orders
            if q in o.username.lower()
            or q in o.human_ref.lower()
            or q in o.order_id.lower()
        ]
        if not matches:
            await _reply(update, f"No orders found matching `{q}`")
        else:
            lines = [f"🔍 *Results for* `{q}`:\n"]
            for o in matches[:10]:
                lines.append(f"• {o.human_ref} @{o.username} — {o.status}")
            await _reply(update, "\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


@admin_only
async def cmd_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A-020: Add internal admin note to an order."""
    args = context.args
    if not args:
        await _reply(update, "Usage: `/addnote <order_id>`")
        return ConversationHandler.END
    context.user_data["note_order_id"] = args[0]
    await _reply(update, f"📝 Enter your internal note for order `{args[0]}`:")
    return STATE_NOTE_TEXT


async def msg_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = context.user_data.pop("note_order_id", "")
    text     = update.effective_message.text.strip()
    actor_id = update.effective_user.id
    try:
        await pool.call(
            "/logistics.LogisticsEngine/AddOrderNote",
            lambda stub, meta: stub.AddOrderNote(
                pb.OrderNoteRequest(order_id=order_id, admin_id=actor_id, text=text),
                metadata=meta,
            ),
            user_id=actor_id,
        )
        await _reply(update, "✅ Note added.")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


async def cb_resolve_issue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show resolution options for an open issue report."""
    query = update.callback_query
    await query.answer()
    order_id = query.data.split(":", 1)[1]
    context.user_data["resolve_order_id"] = order_id

    kb = [
        [InlineKeyboardButton("💰 Refunded",    callback_data=f"do_resolve:{order_id}:refunded")],
        [InlineKeyboardButton("🔄 Replacement", callback_data=f"do_resolve:{order_id}:replacement")],
        [InlineKeyboardButton("❌ Rejected",    callback_data=f"do_resolve:{order_id}:rejected")],
    ]
    await query.edit_message_text(
        "✅ *Resolve Issue — choose outcome:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_RESOLVE_NOTE


async def msg_resolve_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Plain-text resolution note path (inline buttons are the primary path)
    return ConversationHandler.END


# ── Warehouse management ──────────────────────────────────────────────────────

@admin_only
async def cmd_warehouses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-001: List all warehouses."""
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListWarehouses",
            lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
        return

    if not resp.warehouses:
        await _reply(update, "🏭 No warehouses configured.\n\nUse /addwh to add the first one.")
        return

    lines = ["🏭 *Warehouses*\n"]
    for w in resp.warehouses:
        flag = "✅" if w.is_active else "⏸"
        lines.append(
            f"{flag} *{w.name}*\n"
            f"   📍 {w.address}\n"
            f"   ⏰ {w.open_time}–{w.close_time}\n"
            f"   🗺 {w.lat:.4f},{w.lon:.4f}\n"
            f"   ID: `{w.id}`"
        )
    await _reply(update, "\n\n".join(lines))


@admin_only
async def cmd_add_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_wh"] = {}
    await _reply(
        update,
        "🏭 *Add Warehouse*\n\n"
        "Step 1/2 — Enter name and address:\n"
        "Format: `Name | Full street address`",
    )
    return STATE_WH_NAME


async def msg_warehouse_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    if "|" in text:
        name, addr = text.split("|", 1)
        context.user_data["new_wh"]["name"]    = name.strip()
        context.user_data["new_wh"]["address"] = addr.strip()
    else:
        context.user_data["new_wh"]["name"]    = text
        context.user_data["new_wh"]["address"] = text
    await _reply(update, "Step 2/2 — Enter `latitude,longitude`\n(e.g. `6.9271,79.8612`):")
    return STATE_WH_COORDS


async def msg_warehouse_coords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    try:
        lat_s, lon_s = text.split(",", 1)
        lat, lon = float(lat_s.strip()), float(lon_s.strip())
    except (ValueError, TypeError):
        await _reply(update, "❌ Bad format. Enter: `lat,lon`")
        return STATE_WH_COORDS

    d = context.user_data.pop("new_wh", {})
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddWarehouse",
            lambda stub, meta: stub.AddWarehouse(
                pb.Warehouse(
                    name=d.get("name", ""),
                    address=d.get("address", ""),
                    lat=lat, lon=lon,
                    open_time="08:00", close_time="20:00",
                    is_active=True,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update,
            f"✅ Warehouse *{d.get('name')}* added!\nID: `{resp.entity_id}`")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


@admin_only
async def cmd_remove_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, "Usage: `/removewh <warehouse_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveWarehouse",
            lambda stub, meta: stub.RemoveWarehouse(pb.WarehouseId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, "✅ Warehouse removed." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, e)


# ── Driver management ─────────────────────────────────────────────────────────

@admin_only
async def cmd_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-026: Driver list with availability status."""
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListDrivers",
            lambda stub, meta: stub.ListDrivers(
                pb.ListDriversRequest(available_only=False), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
        return

    if not resp.drivers:
        await _reply(update, "🚗 No drivers configured.\n\nUse /adddriver to add one.")
        return

    lines = ["🚗 *Drivers*\n"]
    for d in resp.drivers:
        avail = "🟢 On-duty" if d.is_available else "🔴 Off-duty"
        lines.append(
            f"• *{d.name}* ({d.vehicle_type}) — {avail}\n"
            f"  📱 {d.phone}   Zone: {d.zone or 'Any'}\n"
            f"  ID: `{d.id}`"
        )
    await _reply(update, "\n\n".join(lines))


@admin_only
async def cmd_add_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_driver"] = {}
    await _reply(update, "🚗 *Add Driver*\n\nEnter driver's *full name*:")
    return STATE_DRV_NAME


async def msg_driver_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.setdefault("new_driver", {})["name"] = update.effective_message.text.strip()
    await _reply(update, "Enter driver's *phone number*:")
    return STATE_DRV_PHONE


async def msg_driver_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_driver"]["phone"] = update.effective_message.text.strip()
    d = context.user_data.pop("new_driver", {})
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddDriver",
            lambda stub, meta: stub.AddDriver(
                pb.Driver(
                    name=d.get("name", ""),
                    phone=d.get("phone", ""),
                    vehicle_type="Motorcycle",
                    is_available=False,
                    is_active=True,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, f"✅ Driver *{d.get('name')}* added!\nID: `{resp.entity_id}`")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


@admin_only
async def cmd_remove_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, "Usage: `/removedriver <driver_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveDriver",
            lambda stub, meta: stub.RemoveDriver(pb.DriverId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, "✅ Driver deactivated." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, e)


@admin_only
async def cmd_broadcast_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A-030: Broadcast message to all on-duty drivers."""
    await _reply(update, "📢 Enter the message to broadcast to all on-duty drivers:")
    return STATE_BROADCAST


async def msg_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text.strip()
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/BroadcastDrivers",
            lambda stub, meta: stub.BroadcastDrivers(
                pb.BroadcastRequest(message=text, active_only=True), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, f"✅ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


# ── Promotions ────────────────────────────────────────────────────────────────

@admin_only
async def cmd_promos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-035: Active promotion list."""
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListPromos",
            lambda stub, meta: stub.ListPromos(pb.Empty(), metadata=meta),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
        return

    if not resp.promos:
        await _reply(update, "🏷 No promos configured.\n\nUse /addpromo to create one.")
        return

    lines = ["🏷 *Promotions*\n"]
    for p in resp.promos:
        flag = "✅" if p.is_active else "⏸"
        disc = f"{p.discount_value:.0f}%" if p.discount_type == "percent" \
               else f"LKR {p.discount_value:.0f}"
        uses = f"{p.uses_count}/{p.max_uses}" if p.max_uses else f"{p.uses_count}/∞"
        lines.append(
            f"{flag} *{p.code}* — {disc} off\n"
            f"   Min order: LKR {p.min_order_value:.0f}   Uses: {uses}\n"
            f"   ID: `{p.id}`"
        )
    await _reply(update, "\n\n".join(lines))


@admin_only
async def cmd_add_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(
        update,
        "🏷 *Create Promo Code*\n\n"
        "Format: `CODE | percent|fixed | value | min_order`\n\n"
        "Examples:\n"
        "`SAVE10 | percent | 10 | 1000`\n"
        "`FLAT200 | fixed | 200 | 0`",
    )
    return STATE_PROMO_DEF


async def msg_promo_def(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = [p.strip() for p in update.effective_message.text.split("|")]
    if len(parts) < 3:
        await _reply(update, "❌ Format: `CODE | percent|fixed | value | min_order`")
        return STATE_PROMO_DEF
    try:
        code   = parts[0].upper()
        dtype  = parts[1].lower()
        if dtype not in ("percent", "fixed"):
            raise ValueError("type must be 'percent' or 'fixed'")
        value     = float(parts[2])
        min_order = float(parts[3]) if len(parts) > 3 else 0.0

        resp = await pool.call(
            "/logistics.LogisticsEngine/AddPromo",
            lambda stub, meta: stub.AddPromo(
                pb.Promo(
                    code=code, discount_type=dtype, discount_value=value,
                    min_order_value=min_order, is_active=True,
                    max_uses=0, per_user_limit=0,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, f"✅ Promo *{code}* created! ID: `{resp.entity_id}`")
    except ValueError as e:
        await _reply(update, f"❌ {e}")
        return STATE_PROMO_DEF
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


@admin_only
async def cmd_remove_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, "Usage: `/removepromo <promo_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemovePromo",
            lambda stub, meta: stub.RemovePromo(pb.PromoId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, "✅ Promo removed." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, e)


# ── Loyalty admin ─────────────────────────────────────────────────────────────

@admin_only
async def cmd_adjust_loyalty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(
        update,
        "🏅 *Adjust Loyalty Points*\n\n"
        "Format: `user_id | delta | reason`\n"
        "Example: `123456789 | 100 | Compensation for delay`",
    )
    return STATE_LOYALTY_ADJ


async def msg_loyalty_adjustment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = [p.strip() for p in update.effective_message.text.split("|")]
    if len(parts) < 3:
        await _reply(update, "❌ Format: `user_id | delta | reason`")
        return STATE_LOYALTY_ADJ
    try:
        uid    = int(parts[0])
        delta  = int(parts[1])
        reason = parts[2]
        await pool.call(
            "/logistics.LogisticsEngine/AdjustLoyaltyPoints",
            lambda stub, meta: stub.AdjustLoyaltyPoints(
                pb.LoyaltyAdjustment(
                    user_id=uid, delta=delta, reason=reason,
                    actor_id=update.effective_user.id,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        sign = "+" if delta >= 0 else ""
        await _reply(update,
            f"✅ {sign}{delta} pts for user `{uid}`\nReason: {reason}")
    except (ValueError, IndexError):
        await _reply(update, "❌ Invalid input.")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


# ── Admin user management ─────────────────────────────────────────────────────

@admin_only
async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-055: List all admin users with roles."""
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListAdmins",
            lambda stub, meta: stub.ListAdmins(pb.Empty(), metadata=meta),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
        return

    lines = ["👥 *Admin Users*\n"]
    for a in resp.admins:
        lines.append(f"• `{a.telegram_id}` @{a.username} — *{a.role}*")
    await _reply(update, "\n".join(lines))


@admin_only
async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(
        update,
        "👥 *Add Admin*\n\n"
        "Format: `telegram_id | @username | role`\n"
        "Roles: `super_admin` `dispatcher` `catalog_manager` `support`\n\n"
        "Example: `123456789 | @john | dispatcher`",
    )
    return STATE_NEW_ADMIN


async def msg_new_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = [p.strip() for p in update.effective_message.text.split("|")]
    if len(parts) < 3:
        await _reply(update, "❌ Format: `telegram_id | @username | role`")
        return STATE_NEW_ADMIN

    VALID_ROLES = {"super_admin", "dispatcher", "catalog_manager", "support"}
    try:
        tg_id    = int(parts[0])
        username = parts[1].lstrip("@")
        role     = parts[2].lower()
        if role not in VALID_ROLES:
            await _reply(update,
                f"❌ Invalid role. Use one of: {', '.join(VALID_ROLES)}")
            return STATE_NEW_ADMIN

        await pool.call(
            "/logistics.LogisticsEngine/AddAdmin",
            lambda stub, meta: stub.AddAdmin(
                pb.AdminUser(
                    telegram_id=tg_id, username=username, role=role,
                    added_by=update.effective_user.id,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update,
            f"✅ Admin @{username} added with role *{role}*")
    except (ValueError, IndexError):
        await _reply(update, "❌ Invalid input.")
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END


@admin_only
async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, "Usage: `/removeadmin <telegram_id>`")
        return
    try:
        tg_id = int(args[0])
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveAdmin",
            lambda stub, meta: stub.RemoveAdmin(
                pb.AdminUserId(telegram_id=tg_id), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update,
            "✅ Admin removed." if resp.success else f"❌ {resp.message}")
    except (ValueError, grpc.RpcError) as e:
        await _reply(update, f"⚠️ {e}")


# ── System settings ───────────────────────────────────────────────────────────

@admin_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-057/A-060: Display current system settings."""
    try:
        s = await pool.call(
            "/logistics.LogisticsEngine/GetSystemSettings",
            lambda stub, meta: stub.GetSystemSettings(pb.Empty(), metadata=meta),
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)
        return

    await _reply(
        update,
        "⚙️ *System Settings*\n\n"
        f"`delivery_base_fare`       LKR {s.delivery_base_fare:.0f}\n"
        f"`delivery_rate_per_km`     LKR {s.delivery_rate_per_km:.0f}\n"
        f"`tax_rate`                 {s.tax_rate * 100:.1f}%\n"
        f"`free_delivery_threshold`  LKR {s.free_delivery_threshold:.0f}\n"
        f"`cancel_window_minutes`    {s.cancel_window_minutes} min\n"
        f"`loyalty_earn_rate`        {s.loyalty_earn_rate} pts / LKR 100\n"
        f"`loyalty_redeem_rate`      {s.loyalty_redeem_rate} pts / LKR 1\n"
        f"`min_order_value`          LKR {s.min_order_value}\n"
        f"`max_order_value`          LKR {s.max_order_value} (0=unlimited)\n"
        f"`order_cutoff_time`        {s.order_cutoff_time or 'None'}\n\n"
        "To update, use: `/setsetting <key> <value>`\n"
        "Example: `/setsetting delivery_base_fare 600`",
    )


async def msg_settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Reserved for interactive settings editing flow
    return ConversationHandler.END


# ── Analytics ─────────────────────────────────────────────────────────────────

@admin_only
async def cmd_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-047: Revenue summary for the last 30 days."""
    now = int(time.time())
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetRevenueSummary",
            lambda stub, meta: stub.GetRevenueSummary(
                pb.DateRangeRequest(from_ts=now - 30 * 86400, to_ts=now),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(
            update,
            "💰 *Revenue — Last 30 Days*\n\n"
            f"Total:        LKR {resp.total_revenue:,.0f}\n"
            f"Orders:       {resp.order_count}\n"
            f"Avg order:    LKR {resp.avg_order_value:,.0f}",
        )
    except grpc.RpcError as e:
        await _rpc_error(update, e)


@admin_only
async def cmd_top_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-049: Top 10 selling items by units in last 30 days."""
    now = int(time.time())
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetTopItems",
            lambda stub, meta: stub.GetTopItems(
                pb.TopItemsRequest(from_ts=now - 30 * 86400, to_ts=now, limit=10),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        if not resp.items:
            await _reply(update, "No delivery data yet.")
            return

        lines = ["🏆 *Top Items — Last 30 Days*\n"]
        for i, it in enumerate(resp.items, 1):
            lines.append(
                f"{i}. *{it.name}*\n"
                f"   {it.units_sold} units — LKR {it.revenue:,.0f}"
            )
        await _reply(update, "\n\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, e)


@admin_only
async def cmd_order_trend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-048: Daily order volume for the last 7 days."""
    now = int(time.time())
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetOrderVolumeTrend",
            lambda stub, meta: stub.GetOrderVolumeTrend(
                pb.DateRangeRequest(from_ts=now - 7 * 86400, to_ts=now),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        lines = ["📈 *Order Trend — Last 7 Days*\n"]
        for p in resp.points:
            day = time.strftime("%a %d %b", time.gmtime(p.date_ts))
            bar = "█" * min(p.count, 30)
            lines.append(f"`{day}`: {bar} {p.count}")
        await _reply(update, "\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, e)


@admin_only
async def cmd_low_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-041: Low-stock items dashboard."""
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetLowStockReport",
            lambda stub, meta: stub.GetLowStockReport(
                pb.LowStockRequest(threshold=10), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        if not resp.items:
            await _reply(update, "✅ All items adequately stocked (above threshold 10).")
            return

        lines = ["📉 *Low Stock Report*\n"]
        for it in resp.items:
            icon = "🔴" if it.stock == 0 else "🟡"
            lines.append(
                f"{icon} *{it.name}*\n"
                f"   Stock: {it.stock}   Threshold: {it.threshold}\n"
                f"   ID: `{it.item_id}`"
            )
        await _reply(update, "\n\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, e)


# ── Audit log ─────────────────────────────────────────────────────────────────

@admin_only
async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-056: Last 24 h of audit events."""
    now = int(time.time())
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetAuditLog",
            lambda stub, meta: stub.GetAuditLog(
                pb.AuditLogRequest(
                    from_ts=now - 86400, to_ts=now,
                    page=0, page_size=25,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        if not resp.entries:
            await _reply(update, "📜 No audit events in the last 24 hours.")
            return

        lines = [f"📜 *Audit Log* (last 24h — {resp.total_count} total)\n"]
        for e in resp.entries:
            ts = time.strftime("%H:%M", time.gmtime(e.ts))
            lines.append(
                f"`{ts}` *{e.action}* → `{e.target_id}`\n"
                f"   {e.detail[:80]}"
            )
        await _reply(update, "\n\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, e)


# ── Maintenance ───────────────────────────────────────────────────────────────

@admin_only
async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle maintenance mode on / off immediately."""
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ToggleMaintenance",
            lambda stub, meta: stub.ToggleMaintenance(pb.Empty(), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, f"🔒 {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, e)


@admin_only
async def cmd_schedule_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A-059: Schedule a future maintenance window."""
    await _reply(
        update,
        "🔒 *Schedule Maintenance Window*\n\n"
        "Format: `YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM | reason`\n\n"
        "Example:\n`2026-05-20 02:00 | 2026-05-20 04:00 | Database upgrade`",
    )
    return STATE_MAINT_WINDOW


async def msg_maintenance_window(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    import datetime
    text  = update.effective_message.text.strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2:
        await _reply(update, "❌ Format: `start | end | reason`")
        return STATE_MAINT_WINDOW

    FMT = "%Y-%m-%d %H:%M"
    try:
        start_ts = int(datetime.datetime.strptime(parts[0], FMT).timestamp())
        end_ts   = int(datetime.datetime.strptime(parts[1], FMT).timestamp())
        reason   = parts[2] if len(parts) > 2 else "Scheduled maintenance"

        resp = await pool.call(
            "/logistics.LogisticsEngine/ScheduleMaintenance",
            lambda stub, meta: stub.ScheduleMaintenance(
                pb.MaintenanceWindow(
                    start_ts=start_ts, end_ts=end_ts, reason=reason,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, f"✅ {resp.message}")
    except ValueError:
        await _reply(update, "❌ Date format error. Use: `YYYY-MM-DD HH:MM`")
        return STATE_MAINT_WINDOW
    except grpc.RpcError as e:
        await _rpc_error(update, e)
    return ConversationHandler.END
