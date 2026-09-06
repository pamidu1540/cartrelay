import logging
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import asyncio

logger = logging.getLogger(__name__)

async def delete_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE = None) -> None:
    """Silently delete the user's incoming message to keep chat history clean."""
    msg = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if not msg:
        return
    if context and hasattr(context, "user_data") and isinstance(context.user_data, dict):
        if context.user_data.get("_deleted_msg_id") == msg.message_id:
            return
        context.user_data["_deleted_msg_id"] = msg.message_id
    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Could not delete user message: {e}")

async def send_or_edit_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
    parse_mode=ParseMode.MARKDOWN
) -> None:
    """
    Single Message Interface (SMI) core logic:
    - If triggered by a button (CallbackQuery), edits the existing message.
    - If triggered by a command or text, deletes the previous bot menu (if any) and sends a new one concurrently.
    """
    if len(text) > 4096:
        text = text[:4090] + "\n…"

    # Case 1: Triggered by an inline button (edit only if markup is compatible with inline messages)
    if update.callback_query and not isinstance(reply_markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)):
        try:
            msg = await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
            context.user_data["last_bot_msg_id"] = msg.message_id
            context.user_data["menu_msg_id"] = msg.message_id
            return
        except Exception as e:
            # If the text is exactly the same, Telegram throws an error. Ignore it.
            if "Message is not modified" in str(e):
                return
            if "can't parse entities" in str(e).lower() and parse_mode is not None:
                try:
                    msg = await update.callback_query.edit_message_text(
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=None,
                    )
                    context.user_data["last_bot_msg_id"] = msg.message_id
                    context.user_data["menu_msg_id"] = msg.message_id
                    return
                except Exception:
                    pass
            logger.debug(f"Could not edit message via callback: {e}")
            # Fall back to sending a new one

    # Case 2: Triggered by a command or text (or callback edit failed)
    chat_id = update.effective_chat.id
    last_msg_id = context.user_data.get("last_bot_msg_id")

    # Run old message deletion concurrently with sending the new menu to cut latency in half
    async def _cleanup_old_menu():
        if last_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
            except Exception:
                pass

    del_task = asyncio.create_task(_cleanup_old_menu())

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        context.user_data["menu_msg_id"] = msg.message_id
    except Exception as e:
        if "can't parse entities" in str(e).lower() and parse_mode is not None:
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=None,
                )
                context.user_data["last_bot_msg_id"] = msg.message_id
                context.user_data["menu_msg_id"] = msg.message_id
            except Exception:
                pass
        else:
            logger.error(f"Failed to send main menu: {e}")
    finally:
        await del_task

async def flash_toast(update: Update, text: str, show_alert: bool = False) -> None:
    """
    Show a temporary toast notification using answer_callback_query.
    Only works if triggered by a callback query.
    """
    if update.callback_query:
        try:
            await update.callback_query.answer(text=text, show_alert=show_alert)
        except Exception as e:
            logger.debug(f"Failed to show toast: {e}")
