"""
core.py — Welcome, help, cancellation, support, and background notification polling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import check_maintenance, rate_limit
from handlers.user.common import (
    STATE_NEW_ADDRESS,
    STATE_PROFILE_NAME,
    _cart,
    _safe_reply,
)

logger = logging.getLogger(__name__)


# ── /start ────────────────────────────────────────────────────────────────────

@rate_limit
@check_maintenance
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    user = update.effective_user
    user_id = user.id
    await _cart(context).invalidate_local(user_id)

    # Check cached onboarding status to avoid redundant gRPC roundtrips
    if not context.user_data.get("onboarded"):
        async def _get_profile():
            return await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )

        async def _get_address():
            return await pool.call(
                "/logistics.LogisticsEngine/ListSavedAddresses",
                lambda stub, meta: stub.ListSavedAddresses(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )

        profile_resp, addr_resp = await asyncio.gather(_get_profile(), _get_address(), return_exceptions=True)
        profile = profile_resp if not isinstance(profile_resp, Exception) else None

        if not profile or not profile.display_name or not profile.phone:
            context.user_data["registering"] = True
            context.user_data["reg_profile"] = {"user_id": user_id}

            await send_or_edit_main_menu(
                update,
                context,
                "👋 *Welcome to Logistics Engine!*\n\nLet's set up your profile first.\n\n✏️ *Step 1:* Please enter your full display name:",
            )
            return STATE_PROFILE_NAME

        has_address = False
        if not isinstance(addr_resp, Exception) and getattr(addr_resp, "addresses", None):
            has_address = True

        if not has_address:
            context.user_data["registering"] = True
            reply_kb = [
                [KeyboardButton("📍 Share Location", request_location=True)],
            ]
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "📍 *Delivery Address Required*\n\n"
                    "An address is required to calculate delivery distances and browse catalog items.\n\n"
                    "👉 Please tap *Share Location* below to share your GPS location, or type your address manually:"
                ),
                reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data["last_bot_msg_id"] = msg.message_id
            return STATE_NEW_ADDRESS

        context.user_data["onboarded"] = True

    # Check setup status with cached TTL
    now = time.monotonic()
    setup_cache = context.bot_data.get("_setup_status")
    if setup_cache and (now - setup_cache.get("time", 0.0) < 60):
        has_wh = setup_cache["has_wh"]
        has_cat = setup_cache["has_cat"]
    else:
        async def _check_wh():
            try:
                r = await pool.call("/logistics.LogisticsEngine/ListWarehouses", lambda stub, meta: stub.ListWarehouses(pb.Empty(), metadata=meta))
                return len(r.warehouses) > 0
            except Exception:
                return False

        async def _check_cat():
            try:
                r = await pool.call("/logistics.LogisticsEngine/GetCatalog", lambda stub, meta: stub.GetCatalog(pb.CatalogRequest(page=0, page_size=1), metadata=meta))
                return len(r.items) > 0
            except Exception:
                return False

        has_wh, has_cat = await asyncio.gather(_check_wh(), _check_cat())
        context.bot_data["_setup_status"] = {"has_wh": has_wh, "has_cat": has_cat, "time": now}

    disclaimer = ""
    if not has_wh or not has_cat:
        disclaimer = (
            "⚠️ *Disclaimer:* The system is currently in initial setup mode. "
            "Admin options (such as warehouses or catalog items) have not been fully configured yet.\n\n"
        )

    kb = [
        [InlineKeyboardButton("🛒 Browse Catalog", callback_data="cat_select:all:0")],
        [InlineKeyboardButton("🛍 My Cart",   callback_data="checkout"),
         InlineKeyboardButton("📋 My Orders", callback_data="order_detail:list")],
        [InlineKeyboardButton("👤 Profile",   callback_data="profile_edit:view"),
         InlineKeyboardButton("🎁 Points",    callback_data="loyalty_view")],
        [InlineKeyboardButton("❓ Help",       callback_data="help")],
    ]

    await send_or_edit_main_menu(
        update,
        context,
        f"{disclaimer}👋 Welcome back, *{user.first_name}*!\n\nHow can we help you today?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ConversationHandler.END


# ── Help & Info ───────────────────────────────────────────────────────────────

@rate_limit
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-045: FAQ / help section."""
    text = (
        "❓ *Help & FAQ*\n\n"
        "*How to order*\n"
        "Use /catalog to browse items, add them to your cart, then /cart to checkout.\n\n"
        "*Track my delivery*\n"
        "Use /orders to see your current order status and ETA updates.\n\n"
        "*Cancel an order*\n"
        "Use /orders → tap the order → Cancel (within 5 minutes of placing).\n\n"
        "*Contact support*\n"
        "Use /support to open a live support chat with our team.\n\n"
        "*Promo codes*\n"
        "Enter your promo code at checkout when prompted.\n\n"
        "*Loyalty points*\n"
        "Earn points on every order. Check your balance with /points."
    )
    await send_or_edit_main_menu(update, context, text)


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        "❓ *Help & FAQ*\n\n"
        "*How to order*\n"
        "Use /catalog to browse items, add them to your cart, then /cart to checkout.\n\n"
        "*Track my delivery*\n"
        "Use /orders to see your current order status and ETA updates.\n\n"
        "*Cancel an order*\n"
        "Use /orders → tap the order → Cancel (within 5 minutes of placing).\n\n"
        "*Contact support*\n"
        "Use /support to open a live support chat with our team.\n\n"
        "*Promo codes*\n"
        "Enter your promo code at checkout when prompted.\n\n"
        "*Loyalty points*\n"
        "Earn points on every order. Check your balance with /points."
    )
    await send_or_edit_main_menu(update, context, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    await send_or_edit_main_menu(update, context, "Operation cancelled.")
    return ConversationHandler.END


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_or_edit_main_menu(update, context, "Operation cancelled.")
    return ConversationHandler.END


@rate_limit
async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-046: Live support command."""
    await _safe_reply(
        update,
        context,
        "🆘 *Support*\n\n"
        "Our support team will be with you shortly.\n\n"
        "Please describe your issue and include your order number if applicable "
        "(e.g. ORD-20260513-0047).\n\n"
        "You can also use /report to report an issue with a specific order.",
    )


# ── Notification polling ───────────────────────────────────────────────────────

async def poll_notifications(app) -> None:
    """
    Background task: poll the engine for pending notifications every 30 s
    and forward them to users via Telegram.
    """
    while True:
        await asyncio.sleep(30)
        try:
            admin_resp = await pool.call(
                "/logistics.LogisticsEngine/ListAdmins",
                lambda stub, meta: stub.ListAdmins(pb.Empty(), metadata=meta),
                user_id=0,
            )
            user_ids = {a.telegram_id for a in admin_resp.admins}
            user_ids.update(app.user_data.keys())

            async def _poll_user(uid: int):
                try:
                    notif_resp = await pool.call(
                        "/logistics.LogisticsEngine/GetPendingNotifs",
                        lambda stub, meta: stub.GetPendingNotifs(
                            pb.UserId(user_id=uid), metadata=meta
                        ),
                        user_id=uid,
                    )
                    for notif in notif_resp.notifs:
                        try:
                            await app.bot.send_message(
                                chat_id=uid,
                                text=notif.text,
                                parse_mode=ParseMode.MARKDOWN,
                            )
                        except TelegramError:
                            try:
                                await app.bot.send_message(
                                    chat_id=uid,
                                    text=notif.text,
                                )
                            except TelegramError:
                                pass
                except Exception:
                    pass

            if user_ids:
                await asyncio.gather(*[_poll_user(uid) for uid in user_ids])
        except Exception:
            pass
