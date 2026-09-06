"""
orders.py — Admin order dashboard, status transitions, driver assignment, search, notes, and issue resolution.
"""

from __future__ import annotations

import logging
import time

import grpc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import admin_only
from handlers.admin.common import (
    STATE_NOTE_TEXT,
    STATE_ORDER_SEARCH,
    STATE_RESOLVE_NOTE,
    STATE_STATUS_REASON,
    _reply,
    _rpc_error,
)

logger = logging.getLogger(__name__)


# ── Order Management Handlers ────────────────────────────────────────────────

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
        await _rpc_error(update, context, e)
        return

    if not resp.orders:
        await _reply(update, context, "📋 No orders found.")
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

    kb.append([
        InlineKeyboardButton("All",        callback_data="admin_sf:"),
        InlineKeyboardButton("Pending",    callback_data="admin_sf:pending"),
        InlineKeyboardButton("Dispatched", callback_data="admin_sf:dispatched"),
        InlineKeyboardButton("Issues",     callback_data="admin_sf:issue"),
    ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅", callback_data=f"admin_op:{page-1}"))
    if page < (resp.total_count - 1) // 10:
        nav.append(InlineKeyboardButton("➡", callback_data=f"admin_op:{page+1}"))
    if nav:
        kb.append(nav)

    await _reply(
        update,
        context,
        "\n".join(lines),
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
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")
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

    if order.history:
        lines.append("\n📅 *Timeline:*")
        for h in order.history:
            ts = time.strftime("%H:%M %d %b", time.gmtime(h.ts))
            note = f"  _{h.note}_" if h.note else ""
            lines.append(f"  • `{ts}` {h.status}{note}")

    if order.notes:
        lines.append("\n📝 *Internal Notes:*")
        for n in order.notes:
            ts = time.strftime("%H:%M %d %b", time.gmtime(n.ts))
            lines.append(f"  • `{ts}` {n.text}")

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
    await delete_user_message(update, context)
    msg = update.effective_message
    reason = msg.text.strip() if (msg and msg.text) else "Status updated by admin"
    order_id   = context.user_data.pop("pending_status_order", "")
    new_status = context.user_data.pop("pending_status_value", "")
    actor_id   = update.effective_user.id

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
        await _reply(update, context,
            f"✅ Order moved to *{new_status}*" if resp.success else f"❌ {resp.message}",
        )
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
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
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")
        return

    if not resp.drivers:
        await send_or_edit_main_menu(update, context, "⚠️ No available drivers right now.")
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


async def cb_do_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    order_id = parts[1]
    driver_id = parts[2]
    admin_id = update.effective_user.id if update.effective_user else 0
    try:
        await pool.call(
            "/logistics.LogisticsEngine/AssignDriver",
            lambda stub, meta: stub.AssignDriver(
                pb.AssignDriverRequest(order_id=order_id, driver_id=driver_id, admin_id=admin_id),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await send_or_edit_main_menu(update, context, f"✅ Driver {driver_id} assigned to order {order_id}")
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")


@admin_only
async def cmd_order_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    """A-025: Search orders by customer or order ID."""
    await _reply(update, context, "🔍 Enter customer @username, name, or order number to search:")
    return STATE_ORDER_SEARCH


async def msg_order_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    msg = update.effective_message
    if not msg or not msg.text:
        await _reply(update, context, "❌ Please enter a text search query:")
        return STATE_ORDER_SEARCH
    q = msg.text.strip().lower()
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
            await _reply(update, context, f"No orders found matching `{q}`")
        else:
            lines = [f"🔍 *Results for* `{q}`:\n"]
            for o in matches[:10]:
                lines.append(f"• {o.human_ref} @{o.username} — {o.status}")
            await _reply(update, context, "\n".join(lines))
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
    return ConversationHandler.END


@admin_only
async def cmd_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    """A-020: Add internal admin note to an order."""
    args = context.args
    if not args:
        await _reply(update, context, "Usage: `/addnote <order_id>`")
        return ConversationHandler.END
    context.user_data["note_order_id"] = args[0]
    await _reply(update, context, f"📝 Enter your internal note for order `{args[0]}`:")
    return STATE_NOTE_TEXT


async def msg_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    msg = update.effective_message
    if not msg or not msg.text:
        await _reply(update, context, "❌ Please enter note text:")
        return STATE_NOTE_TEXT
    order_id = context.user_data.pop("note_order_id", "")
    text     = msg.text.strip()
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
        await _reply(update, context, "✅ Note added.")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
    return ConversationHandler.END


async def cb_admin_op(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    context.user_data["admin_orders_page"] = page
    await cmd_all_orders(update, context)


async def cb_admin_sf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    status_f = parts[1] if len(parts) > 1 else ""
    context.user_data["admin_orders_status"] = status_f
    context.user_data["admin_orders_page"] = 0
    await cmd_all_orders(update, context)


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
    await delete_user_message(update, context)
    return ConversationHandler.END


async def cb_do_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    order_id = parts[1]
    resolution = parts[2]
    admin_id = update.effective_user.id if update.effective_user else 0
    try:
        await pool.call(
            "/logistics.LogisticsEngine/ResolveOrderIssue",
            lambda stub, meta: stub.ResolveOrderIssue(
                pb.ResolveIssueRequest(
                    order_id=order_id,
                    admin_id=admin_id,
                    resolution=resolution,
                    note="",
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await send_or_edit_main_menu(update, context, f"✅ Issue resolved: {resolution}")
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")
    return ConversationHandler.END
