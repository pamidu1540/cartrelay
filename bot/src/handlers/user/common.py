"""
common.py — Shared state constants, helpers, and utilities for customer handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cart_manager import CartManager

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from message_manager import send_or_edit_main_menu

logger = logging.getLogger(__name__)

# ConversationHandler states
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

PAGE_SIZE = 10  # items per catalog page


def _cart(context: ContextTypes.DEFAULT_TYPE) -> CartManager:
    return context.application.bot_data["cart_manager"]


async def _update_msg(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
    parse_mode=ParseMode.MARKDOWN,
):
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    chat_id = update.effective_chat.id
    last_msg_id = context.user_data.get("last_bot_msg_id")
    if last_msg_id:
        try:
            if update.callback_query:
                msg = await update.callback_query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
                context.user_data["last_bot_msg_id"] = msg.message_id
                return msg
            else:
                msg = await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=last_msg_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return msg
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
            except Exception:
                pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    context.user_data["last_bot_msg_id"] = msg.message_id
    return msg


async def _safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs) -> None:
    """Reply with parse_mode=Markdown, gracefully handle long messages."""
    msg = update.effective_message
    if not msg:
        return
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await send_or_edit_main_menu(update, context, text, parse_mode=ParseMode.MARKDOWN, **kwargs)


def _order_status_emoji(status: str) -> str:
    return {
        "pending":    "🕐",
        "confirmed":  "✅",
        "dispatched": "🚗",
        "delivered":  "📦",
        "cancelled":  "❌",
        "failed":     "⚠️",
    }.get(status, "❓")
