"""
orders.py — Customer order history, details, cancellation, reordering, and issue reporting.
"""

from __future__ import annotations

import logging
import re

import grpc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from message_manager import send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import rate_limit
from handlers.user.common import _cart, _order_status_emoji

logger = logging.getLogger(__name__)


# ── Order History & Tracking Handlers ────────────────────────────────────────

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
        await send_or_edit_main_menu(update, context, f"⚠️ Could not load orders: {e.details()}")
        return

    if not resp.orders:
        await send_or_edit_main_menu(update, context, "📋 You have no orders yet.\n\nUse /catalog to place your first order!")
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

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Newer", callback_data=f"orders_page:{page - 1}"))
    total_pages = (resp.total_count + 4) // 5
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Older ➡", callback_data=f"orders_page:{page + 1}"))
    if nav:
        kb.append(nav)

    await send_or_edit_main_menu(update, context, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


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
            user_id=update.effective_user.id,
        )
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")
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
    kb.append([InlineKeyboardButton("🔄 Reorder", callback_data=f"reorder:{order_id}")])
    if order.status == "pending":
        kb.append([InlineKeyboardButton("❌ Cancel Order", callback_data=f"confirm_cancel:{order_id}")])
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
    order_id = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        order = await pool.call(
            "/logistics.LogisticsEngine/GetOrder",
            lambda stub, meta: stub.GetOrder(
                pb.OrderId(order_id=order_id), metadata=meta
            ),
            user_id=user_id,
        )
    except grpc.RpcError as e:
        await query.answer(f"⚠️ {e.details()}", show_alert=True)
        return

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
    await send_or_edit_main_menu(update, context, "Please use /orders to find and cancel an order.")


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
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")


async def cmd_report_issue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit_main_menu(update, context, "Please use /orders, tap the delivered order, then tap ⚠️ Report Issue.")


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
                pb.UpdateStatusRequest(
                    order_id=order_id,
                    new_status="issue",
                    reason=f"Customer reported issue: {issue_type}",
                ),
                metadata=meta,
            ),
            user_id=update.effective_user.id,
        )
        await send_or_edit_main_menu(update, context, f"✅ Issue reported ({issue_type}). Our support team will contact you shortly.")
    except grpc.RpcError as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e.details()}")
