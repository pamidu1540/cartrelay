"""
callbacks.py — Typed callback data definitions and parsers for Telegram inline keyboards.
"""

from __future__ import annotations

from enum import StrEnum


class UserCallbackPrefix(StrEnum):
    CATALOG = "cat"
    ITEM = "item"
    CART = "cart"
    CHECKOUT = "chk"
    ORDER = "ord"
    PROFILE = "prof"
    ADDR = "addr"
    NOTIF = "notif"
    HELP = "help"
    PAGE = "page"
    SEARCH = "search"
    PROMO = "promo"


class AdminCallbackPrefix(StrEnum):
    PANEL = "adm"
    ITEMS = "adm_items"
    ORDERS = "adm_orders"
    DRIVERS = "adm_drv"
    PROMOS = "adm_prm"
    SETTINGS = "adm_cfg"
    MAINT = "adm_mnt"
    AUDIT = "adm_aud"
    STATS = "adm_stat"
    ISSUE = "adm_iss"


def build_callback(prefix: str | StrEnum, action: str, *args: str | int) -> str:
    """Build a structured callback string with colon separators."""
    parts = [str(prefix), action] + [str(a) for a in args]
    return ":".join(parts)


def parse_callback(data: str) -> list[str]:
    """Parse a colon-delimited callback string into parts."""
    return data.split(":")
