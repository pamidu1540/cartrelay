"""
profile.py — Customer profile, saved addresses, loyalty balance, language selection, and GDPR account deletion.
"""

from __future__ import annotations

import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import rate_limit
from handlers.user.catalog import _show_catalog_categories_helper
from handlers.user.common import (
    STATE_NEW_ADDRESS,
    STATE_PROFILE_NAME,
    STATE_PROFILE_PHONE,
    _safe_reply,
)

logger = logging.getLogger(__name__)


# ── Profile Handlers ─────────────────────────────────────────────────────────

@rate_limit
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update, context)

    user_id = update.effective_user.id
    try:
        profile = await pool.call(
            "/logistics.LogisticsEngine/GetProfile",
            lambda stub, meta: stub.GetProfile(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        profile = pb.CustomerProfile(user_id=user_id)

    name = profile.display_name or "Not set"
    phone = profile.phone or "Not set"
    lang = profile.language or "en"

    kb = [
        [InlineKeyboardButton("✏️ Edit Name",  callback_data="profile_edit:name")],
        [InlineKeyboardButton("📱 Edit Phone", callback_data="profile_edit:phone")],
        [InlineKeyboardButton("📍 Addresses",  callback_data="profile_edit:addresses")],
        [InlineKeyboardButton("⬅ Back to Menu", callback_data="profile_edit:back_to_main")],
    ]
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"👤 *Your Profile*\n\nName: {name}\nPhone: {phone}\nLanguage: {lang}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["last_bot_msg_id"] = msg.message_id


async def cb_profile_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]

    if field == "view":
        user_id = update.effective_user.id
        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(
                    pb.UserId(user_id=user_id), metadata=meta
                ),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        name = profile.display_name or "Not set"
        phone = profile.phone or "Not set"
        lang = profile.language or "en"

        kb = [
            [InlineKeyboardButton("✏️ Edit Name",  callback_data="profile_edit:name")],
            [InlineKeyboardButton("📱 Edit Phone", callback_data="profile_edit:phone")],
            [InlineKeyboardButton("📍 Addresses",  callback_data="profile_edit:addresses")],
            [InlineKeyboardButton("⬅ Back to Menu", callback_data="profile_edit:back_to_main")],
        ]
        await query.edit_message_text(
            f"👤 *Your Profile*\n\nName: {name}\nPhone: {phone}\nLanguage: {lang}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return ConversationHandler.END

    if field == "back_to_catalog":
        bot = getattr(query, "bot", None)
        if bot is None and hasattr(query, "get_bot"):
            bot = query.get_bot()
        if bot is None:
            bot = context.bot

        if query.message and query.message.photo:
            chat_id = query.message.chat_id
            try:
                await query.message.delete()
            except Exception:
                pass
            if bot:
                query = await bot.send_message(
                    chat_id=chat_id,
                    text="⌛ Loading catalog...",
                )
        await _show_catalog_categories_helper(query, context)
        return ConversationHandler.END

    if field == "back_to_main":
        kb = [
            [InlineKeyboardButton("🛒 Browse Catalog", callback_data="cat_select:all:0")],
            [InlineKeyboardButton("🛍 My Cart",   callback_data="checkout"),
             InlineKeyboardButton("📋 My Orders", callback_data="order_detail:list")],
            [InlineKeyboardButton("👤 Profile",   callback_data="profile_edit:view"),
             InlineKeyboardButton("🎁 Points",    callback_data="loyalty_view")],
            [InlineKeyboardButton("❓ Help",       callback_data="help")],
        ]
        await query.edit_message_text(
            f"👋 Welcome back, *{update.effective_user.first_name}*!\n\nHow can we help you today?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return ConversationHandler.END

    if field == "name":
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="profile_edit:view")]]
        await send_or_edit_main_menu(update, context, "✏️ Enter your new display name:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROFILE_NAME

    if field == "phone":
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="profile_edit:view")]]
        await send_or_edit_main_menu(update, context, "📱 Enter your phone number:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_PROFILE_PHONE

    if field == "addresses":
        user_id = update.effective_user.id
        await _edit_address_list(query, user_id)
        return ConversationHandler.END

    if field == "add_address":
        try:
            await query.message.delete()
        except Exception:
            pass

        reply_kb = [
            [KeyboardButton("📍 Share Location", request_location=True)],
        ]
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📍 Please tap *Share Location* below to share your GPS location, or type your address manually:",
            reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["last_bot_msg_id"] = msg.message_id
        return STATE_NEW_ADDRESS
    return ConversationHandler.END


async def msg_profile_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg or not msg.text:
        await _safe_reply(update, context, "⚠️ Please enter your display name in text:")
        return STATE_PROFILE_NAME
    name = msg.text.strip()
    if not name:
        await _safe_reply(update, context, "⚠️ Please enter a non-empty display name:")
        return STATE_PROFILE_NAME

    try:
        if context.user_data.get("registering"):
            context.user_data.setdefault("reg_profile", {})["display_name"] = name
            await send_or_edit_main_menu(
                update,
                context,
                f"✅ Name saved: *{name}*\n\n📱 *Step 2:* Please enter your phone number:",
            )
            return STATE_PROFILE_PHONE

        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        profile.display_name = name
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
            user_id=user_id,
        )

        await send_or_edit_main_menu(update, context, f"✅ Name updated to *{name}*")
        await cmd_profile(update, context)
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"⚠️ Could not update profile: {e}")
    return ConversationHandler.END


async def msg_profile_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg or not msg.text:
        await _safe_reply(update, context, "⚠️ Please enter your phone number in text:")
        return STATE_PROFILE_PHONE
    phone = msg.text.strip()
    if not phone:
        await _safe_reply(update, context, "⚠️ Please enter a valid phone number:")
        return STATE_PROFILE_PHONE

    try:
        if context.user_data.get("registering"):
            reg = context.user_data.get("reg_profile", {})
            profile = pb.CustomerProfile(
                user_id=user_id,
                display_name=reg.get("display_name", update.effective_user.first_name or "Customer"),
                phone=phone,
            )
            await pool.call(
                "/logistics.LogisticsEngine/UpsertProfile",
                lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
                user_id=user_id,
            )

            reply_kb = [
                [KeyboardButton("📍 Share Location", request_location=True)],
            ]
            msg_out = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"✅ Phone saved: *{phone}*\n\n"
                    "📍 *Step 3:* Awesome! Almost done.\n\n"
                    "An address is required to calculate delivery distances.\n\n"
                    "👉 Please tap *Share Location* below to share your GPS location, or type your address manually:"
                ),
                reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data["last_bot_msg_id"] = msg_out.message_id
            return STATE_NEW_ADDRESS

        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        profile.phone = phone
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
            user_id=user_id,
        )

        await send_or_edit_main_menu(update, context, "✅ Phone number updated")
        await cmd_profile(update, context)
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"⚠️ Could not update profile: {e}")
    return ConversationHandler.END


@rate_limit
async def cmd_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update, context)

    user_id = update.effective_user.id
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        await send_or_edit_main_menu(
            update,
            context,
            "⚠️ Could not load addresses.",
        )
        return

    lines = ["📍 *Saved Addresses*\n"]
    kb = []
    if resp.addresses:
        for a in resp.addresses:
            label = ("⭐ " if a.is_default else "") + a.label
            lines.append(f"• {label}: {a.address}")
            row = [
                InlineKeyboardButton(f"🏠 Default: {a.label}", callback_data=f"addr_default:{a.id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"addr_delete:{a.id}"),
            ]
            kb.append(row)
    else:
        lines.append("No saved addresses.")

    kb.append([InlineKeyboardButton("➕ Add Address", callback_data="profile_edit:add_address")])
    kb.append([InlineKeyboardButton("🔙 Back to Profile", callback_data="profile_edit:view")])

    await send_or_edit_main_menu(
        update,
        context,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _edit_address_list(target, user_id: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    async def edit_msg(text, **kwargs):
        bot = getattr(target, "bot", None)
        if bot is None and hasattr(target, "get_bot"):
            bot = target.get_bot()
        if bot is None and context is not None:
            bot = context.bot
        chat_id = target.message.chat_id if hasattr(target, "message") and target.message is not None else getattr(target, "chat_id", None)
        message_id = target.message.message_id if hasattr(target, "message") and target.message is not None else getattr(target, "message_id", None)
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        await edit_msg("⚠️ Could not load addresses.")
        return

    lines = ["📍 *Saved Addresses*\n"]
    kb = []
    if resp.addresses:
        for a in resp.addresses:
            label = ("⭐ " if a.is_default else "") + a.label
            lines.append(f"• {label}: {a.address}")
            row = [
                InlineKeyboardButton(f"🏠 Default: {a.label}", callback_data=f"addr_default:{a.id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"addr_delete:{a.id}"),
            ]
            kb.append(row)
    else:
        lines.append("No saved addresses.")

    kb.append([InlineKeyboardButton("➕ Add Address", callback_data="profile_edit:add_address")])
    kb.append([InlineKeyboardButton("🔙 Back to Profile", callback_data="profile_edit:view")])

    await edit_msg(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_address_default(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    addr_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        addr = next((a for a in resp.addresses if a.id == addr_id), None)
        if addr:
            addr.is_default = True
            await pool.call(
                "/logistics.LogisticsEngine/SaveAddress",
                lambda stub, meta: stub.SaveAddress(addr, metadata=meta),
                user_id=user_id,
            )
            await query.answer("✅ Default address updated", show_alert=True)
            await _edit_address_list(query, user_id)
        else:
            await query.answer()
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)


async def cb_address_delete(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    addr_id = int(query.data.split(":", 1)[1])
    user_id = update.effective_user.id
    try:
        await pool.call(
            "/logistics.LogisticsEngine/DeleteAddress",
            lambda stub, meta: stub.DeleteAddress(
                pb.AddressId(id=addr_id), metadata=meta
            ),
            user_id=user_id,
        )
        await query.answer("🗑 Address deleted", show_alert=True)
        await _edit_address_list(query, user_id)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)


@rate_limit
async def cmd_add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    reply_kb = [
        [KeyboardButton("📍 Share Location", request_location=True)],
    ]
    await send_or_edit_main_menu(
        update,
        context,
        "📍 Please tap *Share Location* below to share your GPS location, or type your address manually:",
        reply_markup=ReplyKeyboardMarkup(reply_kb, resize_keyboard=True, one_time_keyboard=True),
        parse_mode=ParseMode.MARKDOWN,
    )
    return STATE_NEW_ADDRESS


async def msg_new_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    user_id = update.effective_user.id
    msg = update.effective_message

    try:
        await msg.delete()
    except Exception:
        pass

    lat = 0.0
    lon = 0.0
    if msg.location:
        lat = msg.location.latitude
        lon = msg.location.longitude
        address_text = f"GPS: ({lat:.6f}, {lon:.6f})"
    else:
        if not msg.text:
            await _safe_reply(update, context, "⚠️ Please enter a text address or tap Share Location.")
            return STATE_NEW_ADDRESS
        address_text = msg.text.strip()

    try:
        await pool.call(
            "/logistics.LogisticsEngine/SaveAddress",
            lambda stub, meta: stub.SaveAddress(
                pb.SavedAddress(
                    user_id=user_id,
                    label="Home",
                    address=address_text,
                    lat=lat,
                    lon=lon,
                    is_default=True,
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )

        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data.get("last_bot_msg_id", 0))
        except Exception:
            pass

        if context.user_data.get("registering"):
            context.user_data.pop("registering", None)
            context.user_data["onboarded"] = True

            kb = [
                [InlineKeyboardButton("🛒 Browse Catalog", callback_data="cat_select:all:0")],
                [InlineKeyboardButton("🛍 My Cart",   callback_data="checkout"),
                 InlineKeyboardButton("📋 My Orders", callback_data="order_detail:list")],
                [InlineKeyboardButton("👤 Profile",   callback_data="profile_edit:view"),
                 InlineKeyboardButton("🎁 Points",    callback_data="loyalty_view")],
                [InlineKeyboardButton("❓ Help",       callback_data="help")],
            ]
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🎉 *Account Setup Complete!*\n\nWelcome to CartRelay! Your profile and default address are now configured. You can start shopping immediately:",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.MARKDOWN,
            )
            bot_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"👋 Welcome to CartRelay, *{update.effective_user.first_name}*!\n\nHow can we help you today?",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data["last_bot_msg_id"] = bot_msg.message_id
        else:
            bot_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="✅ Address saved successfully!",
                reply_markup=ReplyKeyboardRemove(),
            )
            context.user_data["last_bot_msg_id"] = bot_msg.message_id
            await _edit_address_list(bot_msg, user_id)

    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ {e}",
            reply_markup=ReplyKeyboardRemove(),
        )
    return ConversationHandler.END


@rate_limit
async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        bal = await pool.call(
            "/logistics.LogisticsEngine/GetLoyaltyBalance",
            lambda stub, meta: stub.GetLoyaltyBalance(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        await _safe_reply(update, context,
            f"🏅 *Loyalty Points*\n\n"
            f"Balance: *{bal.points} points*\n"
            f"Tier: {bal.tier.title()}\n\n"
            f"Earn more points by placing orders!",
        )
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e}")


@rate_limit
async def cb_loyalty_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    try:
        bal = await pool.call(
            "/logistics.LogisticsEngine/GetLoyaltyBalance",
            lambda stub, meta: stub.GetLoyaltyBalance(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        await query.edit_message_text(
            f"🏅 *Loyalty Points*\n\n"
            f"Balance: *{bal.points} points*\n"
            f"Tier: {bal.tier.title()}\n\n"
            f"Earn more points by placing orders!",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"⚠️ Could not load points: {e}")


@rate_limit
async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-041: Referral code."""
    user_id = update.effective_user.id
    code = f"REF{user_id}"
    await _safe_reply(update, context,
        f"🎁 *Refer a Friend*\n\n"
        f"Share your code: `{code}`\n\n"
        f"When a friend places their first order using your code, "
        f"you both earn 100 bonus loyalty points!",
    )


@rate_limit
async def cmd_spending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-032: Monthly spending summary."""
    user_id = update.effective_user.id
    now = int(time.time())
    month_start = now - (now % (30 * 86400))

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetUserOrders",
            lambda stub, meta: stub.GetUserOrders(
                pb.GetUserOrdersRequest(user_id=user_id, page=0, page_size=100),
                metadata=meta,
            ),
            user_id=user_id,
        )
        total = sum(o.total for o in resp.orders if o.timestamp >= month_start)
        count = sum(1 for o in resp.orders if o.timestamp >= month_start)
        await _safe_reply(update, context,
            f"💰 *This Month's Spending*\n\n"
            f"Orders: {count}\n"
            f"Total spent: LKR {total:.0f}\n"
            f"Average order: LKR {total / count:.0f}" if count else "No orders this month.",
        )
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"⚠️ {e}")


@rate_limit
async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-048: Notification preference toggle."""
    user_id = update.effective_user.id
    try:
        prefs = await pool.call(
            "/logistics.LogisticsEngine/GetNotifPrefs",
            lambda stub, meta: stub.GetNotifPrefs(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
    except Exception:
        prefs = pb.NotifPrefs(user_id=user_id, order_updates=True, system_alerts=True)

    def toggle_btn(label: str, field: str, current: bool) -> InlineKeyboardButton:
        icon = "✅" if current else "❌"
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"notif_toggle:{field}")

    kb = [
        [toggle_btn("Order Updates",  "order_updates",  prefs.order_updates)],
        [toggle_btn("Promotions",     "promotions",     prefs.promotions)],
        [toggle_btn("New Products",   "new_products",   prefs.new_products)],
        [toggle_btn("Flash Deals",    "flash_deals",    prefs.flash_deals)],
        [toggle_btn("System Alerts",  "system_alerts",  prefs.system_alerts)],
    ]
    await _safe_reply(update, context, "🔔 *Notification Preferences*\nTap to toggle:",
                      reply_markup=InlineKeyboardMarkup(kb))


async def cb_notif_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    field = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        prefs = await pool.call(
            "/logistics.LogisticsEngine/GetNotifPrefs",
            lambda stub, meta: stub.GetNotifPrefs(pb.UserId(user_id=user_id), metadata=meta),
            user_id=user_id,
        )
        current = getattr(prefs, field, False)
        setattr(prefs, field, not current)
        await pool.call(
            "/logistics.LogisticsEngine/SetNotifPrefs",
            lambda stub, meta: stub.SetNotifPrefs(prefs, metadata=meta),
            user_id=user_id,
        )
        await query.answer("Preference updated!", show_alert=False)
        await cmd_notifications(update, context)
    except Exception as e:
        await query.answer(f"⚠️ {e}", show_alert=True)


@rate_limit
async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """U-049: Language selection."""
    kb = [
        [InlineKeyboardButton("🇬🇧 English",  callback_data="lang_select:en")],
        [InlineKeyboardButton("🇱🇰 සිංහල",  callback_data="lang_select:si")],
        [InlineKeyboardButton("🇱🇰 தமிழ்",   callback_data="lang_select:ta")],
    ]
    await _safe_reply(update, context, "🌐 *Select your language:*",
                      reply_markup=InlineKeyboardMarkup(kb))


async def cb_language_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    lang = query.data.split(":", 1)[1]
    user_id = update.effective_user.id

    try:
        try:
            profile = await pool.call(
                "/logistics.LogisticsEngine/GetProfile",
                lambda stub, meta: stub.GetProfile(pb.UserId(user_id=user_id), metadata=meta),
                user_id=user_id,
            )
        except Exception:
            profile = pb.CustomerProfile(user_id=user_id)

        profile.language = lang
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(profile, metadata=meta),
            user_id=user_id,
        )
        await query.answer()
        await send_or_edit_main_menu(update, context, f"✅ Language set to *{lang}*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await query.answer(f"⚠️ Could not set language: {e}", show_alert=True)


@rate_limit
async def cmd_delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    """Clear profile details and saved addresses; existing orders are retained."""
    kb = [
        [InlineKeyboardButton("⚠️ Yes, delete my data", callback_data="confirm_delete")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ]
    await _safe_reply(update, context,
        "⚠️ *Delete Account*\n\n"
        "This clears your profile name, phone, and saved addresses.\n"
        "Existing orders may retain your Telegram ID and delivery details. "
        "Contact the store operator for further data removal.\n\n"
        "Are you sure?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ConversationHandler.END


async def cb_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    try:
        await pool.call(
            "/logistics.LogisticsEngine/UpsertProfile",
            lambda stub, meta: stub.UpsertProfile(
                pb.CustomerProfile(
                    user_id=user_id,
                    display_name="[deleted]",
                    phone="",
                    language="en",
                ),
                metadata=meta,
            ),
            user_id=user_id,
        )
        addr_resp = await pool.call(
            "/logistics.LogisticsEngine/ListSavedAddresses",
            lambda stub, meta: stub.ListSavedAddresses(
                pb.UserId(user_id=user_id), metadata=meta
            ),
            user_id=user_id,
        )
        for a in addr_resp.addresses:
            addr = a
            await pool.call(
                "/logistics.LogisticsEngine/DeleteAddress",
                lambda stub, meta, _addr=addr: stub.DeleteAddress(
                    pb.AddressId(id=_addr.id), metadata=meta
                ),
                user_id=user_id,
            )

        context.user_data.clear()
        await query.edit_message_text(
            "✅ Your profile and saved addresses have been cleared. Existing order records are retained."
        )
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"⚠️ Could not delete account: {e}")

    return ConversationHandler.END
