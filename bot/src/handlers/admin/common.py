"""
common.py — Shared state constants, helpers, and utilities for admin handlers.
"""

from __future__ import annotations

import logging
import grpc
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from message_manager import send_or_edit_main_menu

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
    STATE_CSV_UPLOAD, STATE_EDIT_ITEM_ID, STATE_EDIT_ITEM_PHOTO,
) = range(31)


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs) -> None:
    msg = update.effective_message
    if not msg:
        return
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await send_or_edit_main_menu(update, context, text, parse_mode=ParseMode.MARKDOWN, **kwargs)


async def _rpc_error(update: Update, context: ContextTypes.DEFAULT_TYPE, exc: grpc.RpcError) -> None:
    await _reply(update, context, f"⚠️ Engine error: {exc.details()}")
