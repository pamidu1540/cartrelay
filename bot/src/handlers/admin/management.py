"""
management.py — Admin dashboard, warehouses, drivers, broadcast, promotions, loyalty, admin users, settings, analytics, audit, and maintenance.
"""

from __future__ import annotations

import datetime
import logging
import re
import time

import grpc
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import admin_only
from handlers.admin.common import (
    STATE_BROADCAST,
    STATE_DRV_NAME,
    STATE_DRV_PHONE,
    STATE_LOYALTY_ADJ,
    STATE_MAINT_WINDOW,
    STATE_NEW_ADMIN,
    STATE_PROMO_DEF,
    STATE_SETTINGS_VAL,
    STATE_WH_COORDS,
    STATE_WH_NAME,
    _reply,
    _rpc_error,
)
from handlers.admin.orders import cmd_all_orders

logger = logging.getLogger(__name__)


# ── Main Admin Panel ──────────────────────────────────────────────────────────

@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
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
    text = "🔧 *Admin Panel*\nSelect a section:"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await _reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def cb_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        return ConversationHandler.END
    elif section == "main":
        await cmd_admin(update, context)
        return ConversationHandler.END
    elif section == "catalog":
        kb = [
            [InlineKeyboardButton("➕ Add Item",    callback_data="item_add:start")],
            [InlineKeyboardButton("📥 Bulk Import", callback_data="bulk_import:start")],
            [InlineKeyboardButton("📉 Low Stock",   callback_data="admin_menu:lowstock")],
            [InlineKeyboardButton("⬅ Back to Menu", callback_data="admin_menu:main")],
        ]
        await query.edit_message_text("📦 *Catalog Management*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END
    elif section == "analytics":
        kb = [
            [InlineKeyboardButton("💰 Revenue",   callback_data="admin_menu:do_revenue")],
            [InlineKeyboardButton("📈 Trend",     callback_data="admin_menu:do_trend")],
            [InlineKeyboardButton("🏆 Top Items", callback_data="admin_menu:do_topitems")],
            [InlineKeyboardButton("⬅ Back to Menu", callback_data="admin_menu:main")],
        ]
        await query.edit_message_text("📊 *Analytics*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END
    elif section == "do_revenue":
        await cmd_revenue(update, context)
        return ConversationHandler.END
    elif section == "do_trend":
        await cmd_order_trend(update, context)
        return ConversationHandler.END
    elif section == "do_topitems":
        await cmd_top_items(update, context)
        return ConversationHandler.END
    elif section == "lowstock":
        await cmd_low_stock(update, context)
        return ConversationHandler.END
    return ConversationHandler.END


# ── Warehouse Management ──────────────────────────────────────────────────────

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
        await _rpc_error(update, context, e)
        return

    lines = ["🏭 *Warehouses*\n"]
    kb = []
    if not resp.warehouses:
        lines.append("_No warehouses configured._")
    else:
        for w in resp.warehouses:
            flag = "✅" if w.is_active else "⏸"
            lines.append(
                f"{flag} *{w.name}*\n"
                f"   📍 {w.address}\n"
                f"   ⏰ {w.open_time}–{w.close_time}\n"
                f"   🗺 {w.lat:.4f},{w.lon:.4f}\n"
                f"   ID: `{w.id}`"
            )
            kb.append([InlineKeyboardButton(f"🗑 Remove {w.name}", callback_data=f"wh_remove:{w.id}")])

    kb.append([InlineKeyboardButton("➕ Add Warehouse", callback_data="wh_add:start")])
    kb.append([InlineKeyboardButton("⬅ Back to Menu", callback_data="admin_menu:main")])

    text = "\n\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await _reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb))


async def cb_wh_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    wh_id = query.data.split(":", 1)[1]
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveWarehouse",
            lambda stub, meta: stub.RemoveWarehouse(pb.WarehouseId(id=wh_id), metadata=meta),
            user_id=update.effective_user.id,
        )
        if resp.success:
            await query.answer("✅ Warehouse removed successfully!")
        else:
            await query.answer(f"❌ {resp.message}", show_alert=True)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)
    await cmd_warehouses(update, context)


async def cb_wh_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await send_or_edit_main_menu(update, context, "❌ Operation cancelled.")
    else:
        await _reply(update, context, "❌ Operation cancelled.")
    context.user_data.pop("new_wh_flow", None)
    context.user_data.pop("new_wh", None)
    return ConversationHandler.END


async def cb_wh_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_wh_flow"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="wh_add:cancel")]]
    await send_or_edit_main_menu(update, context, "🏭 *Add Warehouse Wizard*\n\nStep 1/3 — Enter the warehouse *name*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_WH_NAME


@admin_only
async def cmd_add_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    context.user_data["new_wh_flow"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="wh_add:cancel")]]
    await _reply(update, context, "🏭 *Add Warehouse Wizard*\n\nStep 1/3 — Enter the warehouse *name*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_WH_NAME


async def msg_warehouse_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    flow = context.user_data.get("new_wh_flow")
    if flow is None:
        text = update.effective_message.text.strip()
        if "|" in text:
            name, addr = text.split("|", 1)
            context.user_data["new_wh"] = {"name": name.strip(), "address": addr.strip()}
            kb = [[InlineKeyboardButton("❌ Cancel", callback_data="wh_add:cancel")]]
            await _reply(update, context, "Step 2/2 — Enter `latitude,longitude`:", reply_markup=InlineKeyboardMarkup(kb))
            return STATE_WH_COORDS
        flow = {}
        context.user_data["new_wh_flow"] = flow

    text = update.effective_message.text.strip()
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="wh_add:cancel")]]
    if "name" not in flow:
        flow["name"] = text
        await _reply(update, context, "Step 2/3 — Enter the *full street address* of the warehouse:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_WH_NAME

    if "address" not in flow:
        flow["address"] = text
        reply_kb = [[KeyboardButton("📍 Share Location", request_location=True)]]
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Step 3/3 — 📍 Please share the warehouse *location* (tap the button below, or share a live/static location, or type `latitude,longitude`):",
            reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.MARKDOWN,
        )
        return STATE_WH_COORDS
    return STATE_WH_COORDS


async def msg_warehouse_coords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    msg = update.effective_message
    flow = context.user_data.pop("new_wh_flow", None)
    if flow is None:
        flow = context.user_data.pop("new_wh", {})

    lat, lon = 0.0, 0.0
    if msg.location:
        lat = msg.location.latitude
        lon = msg.location.longitude
    else:
        text = msg.text.strip()
        try:
            lat_s, lon_s = text.split(",", 1)
            lat, lon = float(lat_s.strip()), float(lon_s.strip())
        except (ValueError, TypeError):
            context.user_data["new_wh_flow"] = flow
            kb = [[InlineKeyboardButton("❌ Cancel", callback_data="wh_add:cancel")]]
            await _reply(update, context, "❌ Please send a location attachment or enter in `lat,lon` format:", reply_markup=InlineKeyboardMarkup(kb))
            return STATE_WH_COORDS

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddWarehouse",
            lambda stub, meta: stub.AddWarehouse(
                pb.Warehouse(
                    name=flow.get("name", ""),
                    address=flow.get("address", ""),
                    lat=lat, lon=lon,
                    open_time="08:00", close_time="20:00",
                    is_active=True,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, f"✅ Warehouse *{flow.get('name')}* added successfully!\nID: `{resp.entity_id}`")
    except Exception as e:
        await _reply(update, context, f"❌ Failed to add warehouse: {e}")
    return ConversationHandler.END


@admin_only
async def cmd_remove_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, context, "Usage: `/removewh <warehouse_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveWarehouse",
            lambda stub, meta: stub.RemoveWarehouse(pb.WarehouseId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, "✅ Warehouse removed." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


# ── Driver Management ────────────────────────────────────────────────────────

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
        await _rpc_error(update, context, e)
        return

    lines = ["🚗 *Drivers*\n"]
    kb = []
    if not resp.drivers:
        lines.append("_No drivers configured._")
    else:
        for d in resp.drivers:
            avail = "🟢 On-duty" if d.is_available else "🔴 Off-duty"
            lines.append(
                f"• *{d.name}* ({d.vehicle_type}) — {avail}\n"
                f"  📱 {d.phone}   Zone: {d.zone or 'Any'}\n"
                f"  ID: `{d.id}`"
            )
            kb.append([InlineKeyboardButton(f"🗑 Remove {d.name}", callback_data=f"driver_remove:{d.id}")])

    kb.append([InlineKeyboardButton("➕ Add New Driver", callback_data="driver_add:start")])
    kb.append([InlineKeyboardButton("⬅ Back to Menu", callback_data="admin_menu:main")])

    text = "\n\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await _reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb))


async def cb_driver_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    drv_id = query.data.split(":", 1)[1]
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveDriver",
            lambda stub, meta: stub.RemoveDriver(pb.DriverId(id=drv_id), metadata=meta),
            user_id=update.effective_user.id,
        )
        if resp.success:
            await query.answer("✅ Driver removed successfully!")
        else:
            await query.answer(f"❌ {resp.message}", show_alert=True)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)
    await cmd_drivers(update, context)


async def cb_driver_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await send_or_edit_main_menu(update, context, "❌ Operation cancelled.")
    else:
        await _reply(update, context, "❌ Operation cancelled.")
    context.user_data.pop("new_driver_flow", None)
    context.user_data.pop("new_driver", None)
    return ConversationHandler.END


async def cb_driver_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_driver_flow"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="driver_add:cancel")]]
    await send_or_edit_main_menu(update, context, "🚗 *Add Driver Wizard*\n\nStep 1/3 — Enter driver's *full name*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_DRV_NAME


@admin_only
async def cmd_add_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    context.user_data["new_driver_flow"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="driver_add:cancel")]]
    await _reply(update, context, "🚗 *Add Driver Wizard*\n\nStep 1/3 — Enter driver's *full name*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_DRV_NAME


async def msg_driver_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    flow = context.user_data.get("new_driver_flow")
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="driver_add:cancel")]]
    if flow is None:
        context.user_data.setdefault("new_driver", {})["name"] = update.effective_message.text.strip()
        await _reply(update, context, "Enter driver's *phone number*:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_DRV_PHONE

    flow["name"] = update.effective_message.text.strip()
    await _reply(update, context, "Step 2/3 — Enter driver's *phone number*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_DRV_PHONE


async def msg_driver_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    flow = context.user_data.get("new_driver_flow")
    if flow is None:
        context.user_data["new_driver"]["phone"] = update.effective_message.text.strip()
        d = context.user_data.pop("new_driver", {})
        flow = d
        context.user_data["new_driver_flow"] = flow

    flow["phone"] = update.effective_message.text.strip()

    kb = [
        [
            InlineKeyboardButton("🏍 Motorcycle", callback_data=f"driver_veh:{flow['name']}:{flow['phone']}:Motorcycle"),
            InlineKeyboardButton("🚗 Car / Van", callback_data=f"driver_veh:{flow['name']}:{flow['phone']}:Car"),
        ],
        [
            InlineKeyboardButton("🚲 Bicycle", callback_data=f"driver_veh:{flow['name']}:{flow['phone']}:Bicycle"),
            InlineKeyboardButton("❌ Cancel", callback_data="driver_add:cancel"),
        ],
    ]
    await _reply(update, context, "Step 3/3 — Choose driver's *vehicle type*:", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


async def cb_driver_add_vehicle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    name = parts[1]
    phone = parts[2]
    veh = parts[3]
    context.user_data.pop("new_driver_flow", None)

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddDriver",
            lambda stub, meta: stub.AddDriver(
                pb.Driver(
                    name=name,
                    phone=phone,
                    vehicle_type=veh,
                    is_available=False,
                    is_active=True,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await send_or_edit_main_menu(update, context, f"✅ Driver *{name}* added successfully!\nID: `{resp.entity_id}`")
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"❌ Failed to add driver: {e}")

    await cmd_drivers(update, context)
    return ConversationHandler.END


@admin_only
async def cmd_remove_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, context, "Usage: `/removedriver <driver_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveDriver",
            lambda stub, meta: stub.RemoveDriver(pb.DriverId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, "✅ Driver deactivated." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


@admin_only
async def cmd_broadcast_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    """A-030: Broadcast message to all on-duty drivers."""
    await _reply(update, context, "📢 Enter the message to broadcast to all on-duty drivers:")
    return STATE_BROADCAST


async def msg_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    text = update.effective_message.text.strip()
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/BroadcastDrivers",
            lambda stub, meta: stub.BroadcastDrivers(
                pb.BroadcastRequest(message=text, active_only=True), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, f"✅ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
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
        await _rpc_error(update, context, e)
        return

    lines = ["🏷 *Promotions*\n"]
    kb = []
    if not resp.promos:
        lines.append("_No promos configured._")
    else:
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
            kb.append([InlineKeyboardButton(f"🗑 Remove {p.code}", callback_data=f"promo_remove:{p.id}")])

    kb.append([InlineKeyboardButton("➕ Create Promo Code", callback_data="promo_add:start")])
    kb.append([InlineKeyboardButton("⬅ Back to Menu", callback_data="admin_menu:main")])

    text = "\n\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await _reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb))


async def cb_promo_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    promo_id = query.data.split(":", 1)[1]
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemovePromo",
            lambda stub, meta: stub.RemovePromo(pb.PromoId(id=promo_id), metadata=meta),
            user_id=update.effective_user.id,
        )
        if resp.success:
            await query.answer("✅ Promo removed successfully!")
        else:
            await query.answer(f"❌ {resp.message}", show_alert=True)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)
    await cmd_promos(update, context)


async def cb_promo_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data.pop("new_promo_flow", None)
    await cmd_promos(update, context)
    return ConversationHandler.END


async def cb_promo_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_promo_flow"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
    await query.edit_message_text(
        "🏷 *Create Promo Code Wizard*\n\nStep 1/4 — Enter the *Promo Code* (e.g. SAVE10):",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_PROMO_DEF


@admin_only
async def cmd_add_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    context.user_data["new_promo_flow"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
    await _reply(update, context,
        "🏷 *Create Promo Code Wizard*\n\nStep 1/4 — Enter the *Promo Code* (e.g. SAVE10):",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_PROMO_DEF


async def cb_promo_flow_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dtype = query.data.split(":", 1)[1]
    flow = context.user_data.get("new_promo_flow")

    if not flow or "code" not in flow:
        await send_or_edit_main_menu(update, context, "❌ Session expired.")
        return ConversationHandler.END

    flow["discount_type"] = dtype
    friendly = "percentage value (e.g. 10 for 10% off, must be 1-100)" if dtype == "percent" else "LKR discount amount (e.g. 200, must be positive)"
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
    await query.edit_message_text(
        f"Step 3/4 — Enter the *{friendly}*:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_PROMO_DEF


async def msg_promo_def(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    flow = context.user_data.get("new_promo_flow")
    if flow is None:
        text = update.effective_message.text.strip()
        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) >= 3:
                try:
                    code = parts[0].upper()
                    if not code.isalnum():
                        raise ValueError("Promo code must be alphanumeric.")
                    dtype = parts[1].lower()
                    if dtype not in ("percent", "fixed"):
                        raise ValueError("Discount type must be 'percent' or 'fixed'.")
                    val = float(parts[2])
                    if val <= 0:
                        raise ValueError("Discount value must be positive.")
                    if dtype == "percent" and val > 100:
                        raise ValueError("Percentage discount cannot exceed 100%.")
                    min_order = float(parts[3]) if len(parts) > 3 else 0.0
                    if min_order < 0:
                        raise ValueError("Minimum order value cannot be negative.")
                    await pool.call(
                        "/logistics.LogisticsEngine/AddPromo",
                        lambda stub, meta: stub.AddPromo(
                            pb.Promo(
                                code=code, discount_type=dtype, discount_value=val,
                                min_order_value=min_order, is_active=True,
                                max_uses=0, per_user_limit=0,
                            ),
                            metadata=meta,
                        ),
                        user_id=update.effective_user.id,
                    )
                    await _reply(update, context, f"✅ Promo *{code}* created!")
                except Exception as e:
                    await _reply(update, context, f"❌ Failed: {e}")
                return ConversationHandler.END
        flow = {}
        context.user_data["new_promo_flow"] = flow

    text = update.effective_message.text.strip()
    if "code" not in flow:
        code = text.upper()
        if not code.isalnum():
            kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
            await _reply(update, context, "❌ Promo code must be alphanumeric (letters and numbers only). Please enter again:", reply_markup=InlineKeyboardMarkup(kb))
            return STATE_PROMO_DEF
        flow["code"] = code
        kb = [
            [
                InlineKeyboardButton("Percentage (%)", callback_data="promo_flow_type:percent"),
                InlineKeyboardButton("Fixed Amount (LKR)", callback_data="promo_flow_type:fixed"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")],
        ]
        await _reply(update, context, f"Step 2/4 — Choose discount type for *{flow['code']}*:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROMO_DEF

    if "discount_type" not in flow:
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
        await _reply(update, context, "❌ Please choose a discount type using the buttons above.", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROMO_DEF

    if "value" not in flow:
        try:
            val = float(text)
            if val <= 0:
                raise ValueError("must be positive")

            if flow.get("discount_type") == "percent" and val > 100:
                kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
                await _reply(update, context, "❌ Percentage discount cannot exceed 100%. Please enter a valid percentage (1-100):", reply_markup=InlineKeyboardMarkup(kb))
                return STATE_PROMO_DEF

            flow["value"] = val
            kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
            await _reply(update, context, "Step 4/4 — Enter the *minimum order value* required to apply this promo (LKR, or 0 for none):", reply_markup=InlineKeyboardMarkup(kb))
            return STATE_PROMO_DEF
        except ValueError:
            kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
            if flow.get("discount_type") == "percent":
                await _reply(update, context, "❌ Please enter a valid percentage (1-100):", reply_markup=InlineKeyboardMarkup(kb))
            else:
                await _reply(update, context, "❌ Please enter a valid positive number for LKR discount amount:", reply_markup=InlineKeyboardMarkup(kb))
            return STATE_PROMO_DEF

    try:
        min_order = float(text)
        if min_order < 0:
            raise ValueError("cannot be negative")

        code = flow["code"]
        dtype = flow["discount_type"]
        val = flow["value"]

        resp = await pool.call(
            "/logistics.LogisticsEngine/AddPromo",
            lambda stub, meta: stub.AddPromo(
                pb.Promo(
                    code=code, discount_type=dtype, discount_value=val,
                    min_order_value=min_order, is_active=True,
                    max_uses=0, per_user_limit=0,
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, f"✅ Promo *{code}* created successfully!\nID: `{resp.entity_id}`")
    except ValueError:
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="promo_add:cancel")]]
        await _reply(update, context, "❌ Please enter a valid non-negative number for minimum order value:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROMO_DEF
    except Exception as e:
        await _reply(update, context, f"❌ Failed to create promo: {e}")

    await cmd_promos(update, context)
    return ConversationHandler.END


@admin_only
async def cmd_remove_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, context, "Usage: `/removepromo <promo_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemovePromo",
            lambda stub, meta: stub.RemovePromo(pb.PromoId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, "✅ Promo removed." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


# ── Loyalty Admin ─────────────────────────────────────────────────────────────

@admin_only
async def cmd_adjust_loyalty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    await _reply(update, context,
        "🏅 *Adjust Loyalty Points*\n\n"
        "Format: `user_id | delta | reason`\n"
        "Example: `123456789 | 100 | Compensation for delay`",
    )
    return STATE_LOYALTY_ADJ


async def msg_loyalty_adjustment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    parts = [p.strip() for p in update.effective_message.text.split("|")]
    if len(parts) < 3:
        await _reply(update, context, "❌ Format: `user_id | delta | reason`")
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
        await _reply(update, context,
            f"✅ {sign}{delta} pts for user `{uid}`\nReason: {reason}")
    except (ValueError, IndexError):
        await _reply(update, context, "❌ Invalid input.")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
    return ConversationHandler.END


# ── Admin User Management ─────────────────────────────────────────────────────

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
        await _rpc_error(update, context, e)
        return

    lines = ["👥 *Admin Users*\n"]
    kb = []
    for a in resp.admins:
        lines.append(f"• `{a.telegram_id}` @{a.username} — *{a.role}*")
        if a.telegram_id != update.effective_user.id:
            kb.append([InlineKeyboardButton(f"🗑 Remove @{a.username}", callback_data=f"admin_remove:{a.telegram_id}")])

    kb.append([InlineKeyboardButton("➕ Add New Admin", callback_data="admin_add:start")])
    kb.append([InlineKeyboardButton("⬅ Back to Menu", callback_data="admin_menu:main")])

    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await _reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb))


async def cb_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tg_id = int(query.data.split(":", 1)[1])
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveAdmin",
            lambda stub, meta: stub.RemoveAdmin(
                pb.AdminUserId(telegram_id=tg_id), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        if resp.success:
            await query.answer("✅ Admin removed successfully!")
        else:
            await query.answer(f"❌ {resp.message}", show_alert=True)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)
    await cmd_admins(update, context)


async def cb_admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_admin_flow"] = {}
    await send_or_edit_main_menu(update, context, "👥 *Add Admin Wizard*\n\nStep 1/3 — Please enter the Telegram User ID of the new admin:")
    return STATE_NEW_ADMIN


@admin_only
async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    context.user_data["new_admin_flow"] = {}
    await _reply(update, context, "👥 *Add Admin Wizard*\n\nStep 1/3 — Please enter the Telegram User ID of the new admin:")
    return STATE_NEW_ADMIN


async def msg_new_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    flow = context.user_data.get("new_admin_flow")
    if flow is None:
        text = update.effective_message.text.strip()
        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) >= 3:
                try:
                    tg_id = int(parts[0])
                    username = parts[1].lstrip("@")
                    role = parts[2].lower()
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
                    await _reply(update, context, f"✅ Admin @{username} added with role *{role}*")
                except Exception as e:
                    await _reply(update, context, f"❌ Failed: {e}")
                return ConversationHandler.END
        flow = {}
        context.user_data["new_admin_flow"] = flow

    text = update.effective_message.text.strip()
    if "telegram_id" not in flow:
        try:
            tg_id = int(text)
            flow["telegram_id"] = tg_id
            await _reply(update, context, "Step 2/3 — Enter their Telegram username (with or without @):")
            return STATE_NEW_ADMIN
        except ValueError:
            await _reply(update, context, "❌ User ID must be a number. Please enter a valid Telegram User ID:")
            return STATE_NEW_ADMIN

    if "username" not in flow:
        flow["username"] = text.lstrip("@")
        kb = [
            [
                InlineKeyboardButton("Super Admin", callback_data="add_admin_role:super_admin"),
                InlineKeyboardButton("Dispatcher", callback_data="add_admin_role:dispatcher"),
            ],
            [
                InlineKeyboardButton("Catalog Manager", callback_data="add_admin_role:catalog_manager"),
                InlineKeyboardButton("Support", callback_data="add_admin_role:support"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu:admins")],
        ]
        await _reply(update, context, "Step 3/3 — Choose their admin role:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_NEW_ADMIN
    return STATE_NEW_ADMIN


async def cb_admin_add_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    role = query.data.split(":", 1)[1]
    flow = context.user_data.pop("new_admin_flow", None)

    if not flow or "telegram_id" not in flow or "username" not in flow:
        await send_or_edit_main_menu(update, context, "❌ Session expired.")
        return ConversationHandler.END

    tg_id = flow["telegram_id"]
    username = flow["username"]

    try:
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
        await send_or_edit_main_menu(update, context, f"✅ Admin @{username} added successfully with role *{role}*!", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"❌ Failed to add admin: {e}")

    await cmd_admins(update, context)
    return ConversationHandler.END


@admin_only
async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, context, "Usage: `/removeadmin <telegram_id>`")
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
        await _reply(update, context,
            "✅ Admin removed." if resp.success else f"❌ {resp.message}")
    except (ValueError, grpc.RpcError) as e:
        await _reply(update, context, f"⚠️ {e}")


# ── System Settings ───────────────────────────────────────────────────────────

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
        await _rpc_error(update, context, e)
        return

    text = (
        "⚙️ *System Settings*\n\n"
        f"• *Base Fare:* LKR {s.delivery_base_fare:.0f}\n"
        f"• *Rate Per Km:* LKR {s.delivery_rate_per_km:.0f}\n"
        f"• *Tax Rate:* {s.tax_rate * 100:.1f}%\n"
        f"• *Free Delivery Threshold:* LKR {s.free_delivery_threshold:.0f}\n"
        f"• *Cancel Window:* {s.cancel_window_minutes} min\n"
        f"• *Min Order Value:* LKR {s.min_order_value:.0f}\n"
        f"• *Max Order Value:* LKR {s.max_order_value:.0f} (0=unlim)\n"
        f"• *Order Cutoff Time:* {s.order_cutoff_time or 'None'}\n\n"
        "Tap a setting below to edit its value:"
    )

    kb = [
        [
            InlineKeyboardButton("Base Fare", callback_data="set_key:delivery_base_fare"),
            InlineKeyboardButton("Rate/KM", callback_data="set_key:delivery_rate_per_km"),
        ],
        [
            InlineKeyboardButton("Tax Rate", callback_data="set_key:tax_rate"),
            InlineKeyboardButton("Free Deliv Threshold", callback_data="set_key:free_delivery_threshold"),
        ],
        [
            InlineKeyboardButton("Cancel Window", callback_data="set_key:cancel_window_minutes"),
            InlineKeyboardButton("Min Order Value", callback_data="set_key:min_order_value"),
        ],
        [
            InlineKeyboardButton("Max Order Value", callback_data="set_key:max_order_value"),
            InlineKeyboardButton("Cutoff Time", callback_data="set_key:order_cutoff_time"),
        ],
        [
            InlineKeyboardButton("⬅ Back to Admin Menu", callback_data="admin_menu:main"),
        ],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await _reply(update, context, text, reply_markup=InlineKeyboardMarkup(kb))


async def cb_set_setting_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    context.user_data["editing_setting_key"] = key

    friendly_names = {
        "delivery_base_fare": "Base Fare (LKR)",
        "delivery_rate_per_km": "Rate Per KM (LKR)",
        "tax_rate": "Tax Rate (as decimal, e.g. 0.15 for 15%)",
        "free_delivery_threshold": "Free Delivery Threshold (LKR)",
        "cancel_window_minutes": "Cancel Window (minutes)",
        "min_order_value": "Min Order Value (LKR)",
        "max_order_value": "Max Order Value (LKR)",
        "order_cutoff_time": "Order Cutoff Time (HH:MM format, or 'None')",
    }

    friendly = friendly_names.get(key, key)
    await send_or_edit_main_menu(update, context, f"✏️ *Editing System Setting*\n\nPlease enter the new value for *{friendly}*:")
    return STATE_SETTINGS_VAL


async def msg_settings_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    key = context.user_data.pop("editing_setting_key", None)
    if not key:
        await _reply(update, context, "❌ Setting session expired.")
        return ConversationHandler.END

    val_text = update.effective_message.text.strip()
    try:
        s = await pool.call(
            "/logistics.LogisticsEngine/GetSystemSettings",
            lambda stub, meta: stub.GetSystemSettings(pb.Empty(), metadata=meta),
            user_id=update.effective_user.id,
        )
    except Exception as e:
        await _reply(update, context, f"❌ Failed to fetch current settings: {e}")
        return ConversationHandler.END

    try:
        if key == "order_cutoff_time":
            if val_text.lower() == "none" or val_text == "":
                s.order_cutoff_time = ""
            else:
                if not re.match(r"^\d{2}:\d{2}$", val_text):
                    raise ValueError("Cutoff time must be in HH:MM format.")
                s.order_cutoff_time = val_text
        elif key in ("delivery_base_fare", "delivery_rate_per_km", "tax_rate", "free_delivery_threshold"):
            val = float(val_text)
            if val < 0:
                raise ValueError("Value cannot be negative.")
            setattr(s, key, val)
        elif key in ("cancel_window_minutes", "min_order_value", "max_order_value"):
            val = int(val_text)
            if val < 0:
                raise ValueError("Value cannot be negative.")
            setattr(s, key, val)
    except ValueError as e:
        await _reply(update, context, f"❌ Invalid value: {e}. Please try again.")
        context.user_data["editing_setting_key"] = key
        return STATE_SETTINGS_VAL

    try:
        await pool.call(
            "/logistics.LogisticsEngine/SetSystemSettings",
            lambda stub, meta: stub.SetSystemSettings(s, metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, f"✅ Setting *{key}* updated successfully!")
    except Exception as e:
        await _reply(update, context, f"❌ Failed to save settings: {e}")

    await cmd_settings(update, context)
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
        await _reply(update, context,
            "💰 *Revenue — Last 30 Days*\n\n"
            f"Total:        LKR {resp.total_revenue:,.0f}\n"
            f"Orders:       {resp.order_count}\n"
            f"Avg order:    LKR {resp.avg_order_value:,.0f}",
        )
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


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
            await _reply(update, context, "No delivery data yet.")
            return

        lines = ["🏆 *Top Items — Last 30 Days*\n"]
        for i, it in enumerate(resp.items, 1):
            lines.append(
                f"{i}. *{it.name}*\n"
                f"   {it.units_sold} units — LKR {it.revenue:,.0f}"
            )
        await _reply(update, context, "\n\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


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
        await _reply(update, context, "\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


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
            await _reply(update, context, "✅ All items adequately stocked (above threshold 10).")
            return

        lines = ["📉 *Low Stock Report*\n"]
        for it in resp.items:
            icon = "🔴" if it.stock == 0 else "🟡"
            lines.append(
                f"{icon} *{it.name}*\n"
                f"   Stock: {it.stock}   Threshold: {it.threshold}\n"
                f"   ID: `{it.item_id}`"
            )
        await _reply(update, context, "\n\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


# ── Audit Log ─────────────────────────────────────────────────────────────────

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
            await _reply(update, context, "📜 No audit events in the last 24 hours.")
            return

        lines = [f"📜 *Audit Log* (last 24h — {resp.total_count} total)\n"]
        for e in resp.entries:
            ts = time.strftime("%H:%M", time.gmtime(e.ts))
            lines.append(
                f"`{ts}` *{e.action}* → `{e.target_id}`\n"
                f"   {e.detail[:80]}"
            )
        await _reply(update, context, "\n\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


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
        await _reply(update, context, f"🔒 {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


@admin_only
async def cmd_schedule_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    """A-059: Schedule a future maintenance window."""
    await _reply(update, context,
        "🔒 *Schedule Maintenance Window*\n\n"
        "Format: `YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM | reason`\n\n"
        "Example:\n`2026-05-20 02:00 | 2026-05-20 04:00 | Database upgrade`",
    )
    return STATE_MAINT_WINDOW


async def msg_maintenance_window(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    text  = update.effective_message.text.strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2:
        await _reply(update, context, "❌ Format: `start | end | reason`")
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
        await _reply(update, context, f"✅ {resp.message}")
    except ValueError:
        await _reply(update, context, "❌ Date format error. Use: `YYYY-MM-DD HH:MM`")
        return STATE_MAINT_WINDOW
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
    return ConversationHandler.END
