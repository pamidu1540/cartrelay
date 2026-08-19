"""
user_keyboards.py — Standard inline keyboard generators for customer-facing flows.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_catalog_keyboard(
    items: list,
    page: int,
    total_pages: int,
    cart_count: int = 0,
) -> InlineKeyboardMarkup:
    """Builds a paginated catalog navigation keyboard."""
    buttons: list[list[InlineKeyboardButton]] = []

    # 2 columns of item buttons
    row: list[InlineKeyboardButton] = []
    for item in items:
        label = f"{item.name} · LKR {item.price:.0f}"
        if item.stock <= 0:
            label = f"❌ {item.name} (Out of stock)"
        row.append(InlineKeyboardButton(text=label, callback_data=f"item:{item.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Navigation row
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cat_page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{max(1, total_pages)}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"cat_page:{page + 1}"))
    buttons.append(nav_row)

    # Action row
    cart_label = f"🛒 View Cart ({cart_count})" if cart_count > 0 else "🛒 View Cart"
    buttons.append([
        InlineKeyboardButton(cart_label, callback_data="view_cart"),
        InlineKeyboardButton("🔍 Search", callback_data="search_prompt"),
    ])
    buttons.append([
        InlineKeyboardButton("👤 My Profile", callback_data="profile_menu"),
        InlineKeyboardButton("📦 My Orders", callback_data="my_orders:0"),
    ])

    return InlineKeyboardMarkup(buttons)


def build_item_keyboard(
    item_id: str,
    in_cart_qty: int,
    stock: int,
    is_subscribed: bool = False,
) -> InlineKeyboardMarkup:
    """Builds the single-item detail view and quantity modifier keyboard."""
    buttons: list[list[InlineKeyboardButton]] = []

    if stock > 0:
        qty_row = [
            InlineKeyboardButton("➖", callback_data=f"cart_dec:{item_id}"),
            InlineKeyboardButton(f"🛒 {in_cart_qty} in cart", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"cart_inc:{item_id}"),
        ]
        buttons.append(qty_row)
    else:
        sub_label = "🔕 Cancel Restock Alert" if is_subscribed else "🔔 Notify When in Stock"
        sub_action = f"unsub_stock:{item_id}" if is_subscribed else f"sub_stock:{item_id}"
        buttons.append([InlineKeyboardButton(sub_label, callback_data=sub_action)])

    buttons.append([
        InlineKeyboardButton("⬅️ Back to Catalog", callback_data="cat_page:0"),
        InlineKeyboardButton("🛒 View Cart", callback_data="view_cart"),
    ])
    return InlineKeyboardMarkup(buttons)


def build_cart_keyboard(has_items: bool) -> InlineKeyboardMarkup:
    """Builds the cart view actions keyboard."""
    buttons: list[list[InlineKeyboardButton]] = []
    if has_items:
        buttons.append([
            InlineKeyboardButton("🚀 Proceed to Checkout", callback_data="start_checkout"),
        ])
        buttons.append([
            InlineKeyboardButton("🏷️ Apply Promo Code", callback_data="enter_promo"),
            InlineKeyboardButton("🗑️ Clear Cart", callback_data="clear_cart_confirm"),
        ])
    buttons.append([
        InlineKeyboardButton("🛍️ Continue Shopping", callback_data="cat_page:0"),
    ])
    return InlineKeyboardMarkup(buttons)


def build_checkout_keyboard() -> InlineKeyboardMarkup:
    """Builds the checkout confirmation keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Place Order", callback_data="confirm_order")],
        [InlineKeyboardButton("📍 Change Address", callback_data="pick_address")],
        [InlineKeyboardButton("⬅️ Back to Cart", callback_data="view_cart")],
    ])


def build_profile_keyboard() -> InlineKeyboardMarkup:
    """Builds the customer profile and preferences keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Saved Addresses", callback_data="list_addresses")],
        [InlineKeyboardButton("🔔 Notification Preferences", callback_data="notif_prefs")],
        [InlineKeyboardButton("🎁 Loyalty Points & Rewards", callback_data="view_loyalty")],
        [InlineKeyboardButton("⬅️ Back to Catalog", callback_data="cat_page:0")],
    ])
