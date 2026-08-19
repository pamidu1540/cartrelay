"""
admin_keyboards.py — Standard inline keyboard generators for administrator dashboard flows.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_admin_main_keyboard() -> InlineKeyboardMarkup:
    """Builds the main admin dashboard menu keyboard."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Orders Management", callback_data="admin_orders:pending"),
            InlineKeyboardButton("🏷️ Catalog Items", callback_data="admin_items:0"),
        ],
        [
            InlineKeyboardButton("🚚 Drivers Dispatch", callback_data="admin_drivers"),
            InlineKeyboardButton("🏢 Warehouses", callback_data="admin_warehouses"),
        ],
        [
            InlineKeyboardButton("📊 Analytics & Trends", callback_data="admin_analytics"),
            InlineKeyboardButton("🎟️ Promotions", callback_data="admin_promos"),
        ],
        [
            InlineKeyboardButton("⚙️ System Config", callback_data="admin_settings"),
            InlineKeyboardButton("🛡️ Audit Log", callback_data="admin_audit:0"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="admin_main"),
        ],
    ])


def build_order_action_keyboard(
    order_id: str,
    status: str,
    driver_assigned: bool = False,
) -> InlineKeyboardMarkup:
    """Builds action buttons for managing a specific order in the admin dashboard."""
    buttons: list[list[InlineKeyboardButton]] = []

    if status == "pending":
        buttons.append([
            InlineKeyboardButton("✅ Confirm Order", callback_data=f"adm_ord_status:{order_id}:confirmed"),
            InlineKeyboardButton("❌ Cancel Order", callback_data=f"adm_ord_status:{order_id}:cancelled"),
        ])
    elif status == "confirmed":
        buttons.append([
            InlineKeyboardButton("🚚 Dispatch", callback_data=f"adm_ord_status:{order_id}:dispatched"),
            InlineKeyboardButton("👤 Assign Driver", callback_data=f"adm_ord_driver:{order_id}"),
        ])
    elif status == "dispatched":
        buttons.append([
            InlineKeyboardButton("🎉 Mark Delivered", callback_data=f"adm_ord_status:{order_id}:delivered"),
            InlineKeyboardButton("⚠️ Mark Failed", callback_data=f"adm_ord_status:{order_id}:failed"),
        ])

    buttons.append([
        InlineKeyboardButton("📝 Add Note", callback_data=f"adm_ord_note:{order_id}"),
        InlineKeyboardButton("⬅️ Back to Orders", callback_data="admin_orders:pending"),
    ])
    return InlineKeyboardMarkup(buttons)


def build_stock_edit_keyboard(item_id: str, current_stock: int) -> InlineKeyboardMarkup:
    """Builds rapid stock adjustment buttons for catalog management."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("-10", callback_data=f"adm_stk:{item_id}:-10"),
            InlineKeyboardButton("-1", callback_data=f"adm_stk:{item_id}:-1"),
            InlineKeyboardButton(f"Stock: {current_stock}", callback_data="noop"),
            InlineKeyboardButton("+1", callback_data=f"adm_stk:{item_id}:1"),
            InlineKeyboardButton("+10", callback_data=f"adm_stk:{item_id}:10"),
        ],
        [
            InlineKeyboardButton("💵 Edit Price", callback_data=f"adm_price:{item_id}"),
            InlineKeyboardButton("🗑️ Remove Item", callback_data=f"adm_del_item:{item_id}"),
        ],
        [
            InlineKeyboardButton("⬅️ Back to Catalog", callback_data="admin_items:0"),
        ],
    ])
