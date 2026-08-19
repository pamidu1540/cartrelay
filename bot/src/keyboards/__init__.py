"""
keyboards — Modular Telegram Inline Keyboard builders.
"""

from .user_keyboards import (
    build_catalog_keyboard,
    build_item_keyboard,
    build_cart_keyboard,
    build_checkout_keyboard,
    build_profile_keyboard,
)
from .admin_keyboards import (
    build_admin_main_keyboard,
    build_order_action_keyboard,
    build_stock_edit_keyboard,
)

__all__ = [
    "build_catalog_keyboard",
    "build_item_keyboard",
    "build_cart_keyboard",
    "build_checkout_keyboard",
    "build_profile_keyboard",
    "build_admin_main_keyboard",
    "build_order_action_keyboard",
    "build_stock_edit_keyboard",
]
