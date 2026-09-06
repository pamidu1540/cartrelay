from __future__ import annotations

# ── Datadog Tracer Initialization ─────────────────────────────────────────────
import os
try:
    if os.environ.get("DD_AGENT_HOST") or os.environ.get("DD_API_KEY"):
        from ddtrace import patch_all, config
        patch_all(logging=True)
        # Configure global tags
        config.env = os.environ.get("DD_ENV", "production")
        config.service = os.environ.get("DD_SERVICE", "logistics-bot")
        config.version = os.environ.get("APP_VERSION", "11.0.0")
except ImportError:
    pass

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from admin_handlers import (
    # catalog
    cmd_admin,
    cmd_add_item,
    cb_admin_menu,
    cb_item_add_start,
    cb_item_category_callback,
    cb_item_photo_skip,
    cb_item_desc_skip,
    cb_item_cancel,
    msg_item_name,
    msg_item_price,
    msg_item_stock,
    msg_item_category,
    msg_item_photo,
    msg_item_description,
    cmd_edit_item,
    cmd_remove_item,
    msg_edit_item_id,
    msg_edit_item_photo,
    cb_edit_item_photo_keep,
    cmd_bulk_import,
    cb_bulk_import_start,
    msg_csv_upload,
    # orders
    cmd_all_orders,
    cb_admin_order_detail,
    cb_admin_update_status,
    msg_status_reason,
    cb_admin_assign_driver,
    cmd_order_search,
    msg_order_search_query,
    cmd_add_note,
    msg_note_text,
    cb_admin_op,
    cb_admin_sf,
    cb_do_assign,
    cb_do_resolve,
    # warehouses
    cmd_warehouses,
    cmd_add_warehouse,
    msg_warehouse_name,
    msg_warehouse_coords,
    cmd_remove_warehouse,
    # drivers
    cmd_drivers,
    cmd_add_driver,
    msg_driver_name,
    msg_driver_phone,
    cmd_remove_driver,
    cmd_broadcast_drivers,
    msg_broadcast_text,
    # promos
    cmd_promos,
    cmd_add_promo,
    msg_promo_def,
    cmd_remove_promo,
    # loyalty admin
    cmd_adjust_loyalty,
    msg_loyalty_adjustment,
    # admins
    cmd_admins,
    cmd_add_admin,
    msg_new_admin,
    cmd_remove_admin,
    # settings
    cmd_settings,
    msg_settings_value,
    # analytics
    cmd_revenue,
    cmd_top_items,
    cmd_order_trend,
    cmd_low_stock,
    # audit
    cmd_audit,
    # maintenance
    cmd_maintenance,
    cmd_schedule_maintenance,
    msg_maintenance_window,
    # resolve issue
    cb_resolve_issue,
    msg_resolve_note,
    # wizard callbacks
    cb_wh_remove,
    cb_wh_add_start,
    cb_wh_cancel,
    cb_driver_remove,
    cb_driver_add_start,
    cb_driver_add_vehicle,
    cb_driver_cancel,
    cb_admin_remove,
    cb_admin_add_start,
    cb_admin_add_role,
    cb_promo_remove,
    cb_promo_add_start,
    cb_promo_flow_type,
    cb_promo_cancel,
    cb_set_setting_key,
)
from cart_manager import CartManager
from grpc_client import pool
from user_handlers import (
    # core
    cmd_start,
    cmd_help,
    cmd_cancel,
    # catalog
    cmd_catalog,
    cb_catalog_page,
    cb_category_select,
    cb_item_detail,
    cb_add_to_cart,
    cb_stock_notify,
    # cart
    cmd_cart,
    cb_qty_plus,
    cb_qty_minus,
    cb_remove_item,
    cb_clear_cart,
    cb_checkout,
    # quote + order
    cb_confirm_order,
    msg_delivery_address,
    cb_address_select,
    cb_use_promo,
    msg_promo_code,
    cb_slot_select,
    # order management
    cmd_orders,
    cb_order_detail,
    cb_reorder,
    cb_confirm_cancel,
    cb_issue_type,
    cb_issue_submit,
    cb_orders_page,
    cb_orders_list,
    # profile
    cmd_profile,
    cb_profile_edit,
    msg_profile_name,
    msg_profile_phone,
    cmd_addresses,
    cb_address_default,
    cb_address_delete,
    msg_new_address,
    cmd_add_address,
    cmd_cancel_order,
    cmd_report_issue,
    cb_cancel,
    # loyalty
    cmd_points,
    cb_loyalty_view,
    # promo referral
    cmd_refer,
    # notifications
    cmd_notifications,
    cb_notif_toggle,
    # language
    cmd_language,
    cb_language_select,
    # spending
    cmd_spending,
    # gdpr
    cmd_delete_account,
    cb_confirm_delete,
    # support
    cmd_support,
    # notif poll
    poll_notifications,
    # callbacks
    cb_help,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── ConversationHandler states ────────────────────────────────────────────────
# Defined centrally so user and admin handlers can share them.

(
    # User checkout flow
    STATE_ADDRESS,
    STATE_PROMO,
    STATE_SLOT,
    STATE_CONFIRM,
    # Admin item flow
    STATE_ITEM_NAME,
    STATE_ITEM_PRICE,
    STATE_ITEM_STOCK,
    STATE_ITEM_CATEGORY,
    STATE_ITEM_PHOTO,
    STATE_ITEM_DESC,
    # Admin order flow
    STATE_STATUS_REASON,
    STATE_NOTE_TEXT,
    STATE_ORDER_SEARCH,
    # Admin warehouse flow
    STATE_WH_NAME,
    STATE_WH_COORDS,
    # Admin driver flow
    STATE_DRV_NAME,
    STATE_DRV_PHONE,
    # Admin promo flow
    STATE_PROMO_DEF,
    # Admin broadcast
    STATE_BROADCAST,
    # Admin loyalty
    STATE_LOYALTY_ADJ,
    # Admin add-admin
    STATE_NEW_ADMIN,
    # Admin settings
    STATE_SETTINGS_VAL,
    # Admin maintenance
    STATE_MAINT_WINDOW,
    # Admin resolve issue
    STATE_RESOLVE_NOTE,
    # Profile editing
    STATE_PROFILE_NAME,
    STATE_PROFILE_PHONE,
    # Address
    STATE_NEW_ADDRESS,
    # Promo code entry
    STATE_PROMO_CODE,
    # CSV upload
    STATE_CSV_UPLOAD,
    # Edit item flow
    STATE_EDIT_ITEM_ID,
    STATE_EDIT_ITEM_PHOTO,
) = range(31)


# ── Application factory ────────────────────────────────────────────────────────

def build_application(bot: Any = None) -> Application:
    builder = ApplicationBuilder().post_init(post_init).post_shutdown(post_shutdown)
    if bot is not None:
        builder = builder.bot(bot).updater(None)
    else:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        builder = builder.token(token)

    app = builder.build()

    # Attach cart manager to bot_data for access across handlers
    app.bot_data["cart_manager"] = CartManager()

    # ── User conversation handler ──────────────────────────────────────────
    user_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",   cmd_start),
            CommandHandler("catalog", cmd_catalog),
            CommandHandler("cart",    cmd_cart),
            CommandHandler("orders",  cmd_orders),
            CommandHandler("checkout",cmd_cart),
            CommandHandler("addaddress", cmd_add_address),
            CallbackQueryHandler(cb_profile_edit, pattern=r"^profile_edit:"),
            CallbackQueryHandler(cb_checkout, pattern=r"^checkout$"),
        ],
        states={
            STATE_ADDRESS: [
                MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, msg_delivery_address),
                CallbackQueryHandler(cb_address_select, pattern=r"^addr_select:"),
            ],
            STATE_PROMO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_promo_code),
                CallbackQueryHandler(cb_use_promo,  pattern=r"^promo:"),
            ],
            STATE_SLOT: [
                CallbackQueryHandler(cb_slot_select, pattern=r"^slot:"),
            ],
            STATE_CONFIRM: [
                CallbackQueryHandler(cb_confirm_order, pattern=r"^confirm_order(_contactless)?$"),
                CallbackQueryHandler(cmd_cancel,       pattern=r"^cancel_order$"),
            ],
            STATE_PROFILE_NAME:  [
                CallbackQueryHandler(cb_profile_edit, pattern=r"^profile_edit:"),
                MessageHandler(filters.ALL & ~filters.COMMAND, msg_profile_name)
            ],
            STATE_PROFILE_PHONE: [
                CallbackQueryHandler(cb_profile_edit, pattern=r"^profile_edit:"),
                MessageHandler(filters.ALL & ~filters.COMMAND, msg_profile_phone)
            ],
            STATE_NEW_ADDRESS:   [MessageHandler(filters.ALL & ~filters.COMMAND, msg_new_address)],
            STATE_PROMO_CODE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_promo_code)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_start),  # BUG-030: reset state on /start
        ],
        allow_reentry=True,
        per_message=False,
    )

    # ── Admin conversation handler ─────────────────────────────────────────
    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin",     cmd_admin),
            CommandHandler("additem",   cmd_add_item),
            CommandHandler("edititem",  cmd_edit_item),
            CommandHandler("addwh",     cmd_add_warehouse),
            CommandHandler("adddriver", cmd_add_driver),
            CommandHandler("addpromo",  cmd_add_promo),
            CommandHandler("addadmin",  cmd_add_admin),
            CommandHandler("broadcast", cmd_broadcast_drivers),
            CommandHandler("maint",     cmd_schedule_maintenance),
            CommandHandler("bulkimport",cmd_bulk_import),
            CommandHandler("ordersearch",cmd_order_search),
            CommandHandler("addnote",   cmd_add_note),
            CommandHandler("adjustloyalty", cmd_adjust_loyalty),
            CallbackQueryHandler(cb_wh_add_start, pattern=r"^wh_add:start$"),
            CallbackQueryHandler(cb_driver_add_start, pattern=r"^driver_add:start$"),
            CallbackQueryHandler(cb_promo_add_start, pattern=r"^promo_add:start$"),
            CallbackQueryHandler(cb_admin_add_start, pattern=r"^admin_add:start$"),
            CallbackQueryHandler(cb_set_setting_key, pattern=r"^set_key:"),
            CallbackQueryHandler(cb_item_add_start, pattern=r"^item_add:start$"),
            CallbackQueryHandler(cb_bulk_import_start, pattern=r"^bulk_import:start$"),
            CallbackQueryHandler(cb_admin_menu, pattern=r"^admin_menu:"),
        ],
        states={
            STATE_ITEM_NAME:    [
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_item_name)
            ],
            STATE_ITEM_PRICE:   [
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_item_price)
            ],
            STATE_ITEM_STOCK:   [
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_item_stock)
            ],
            STATE_ITEM_CATEGORY:[
                CallbackQueryHandler(cb_item_category_callback, pattern=r"^item_cat_select:"),
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_item_category)
            ],
            STATE_ITEM_PHOTO:   [
                CallbackQueryHandler(cb_item_photo_skip, pattern=r"^item_photo_skip$"),
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), msg_item_photo)
            ],
            STATE_ITEM_DESC:    [
                CallbackQueryHandler(cb_item_desc_skip, pattern=r"^item_desc_skip$"),
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_item_description)
            ],
            STATE_STATUS_REASON:[MessageHandler(filters.TEXT & ~filters.COMMAND, msg_status_reason)],
            STATE_NOTE_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_note_text)],
            STATE_ORDER_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_order_search_query)],
            STATE_WH_NAME:      [
                CallbackQueryHandler(cb_wh_cancel, pattern=r"^wh_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_warehouse_name)
            ],
            STATE_WH_COORDS:    [
                CallbackQueryHandler(cb_wh_cancel, pattern=r"^wh_add:cancel$"),
                MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), msg_warehouse_coords)
            ],
            STATE_DRV_NAME:     [
                CallbackQueryHandler(cb_driver_cancel, pattern=r"^driver_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_driver_name)
            ],
            STATE_DRV_PHONE:    [
                CallbackQueryHandler(cb_driver_cancel, pattern=r"^driver_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_driver_phone)
            ],
            STATE_PROMO_DEF:    [
                CallbackQueryHandler(cb_promo_cancel, pattern=r"^promo_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_promo_def)
            ],
            STATE_BROADCAST:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_broadcast_text)],
            STATE_LOYALTY_ADJ:  [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_loyalty_adjustment)],
            STATE_NEW_ADMIN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_new_admin)],
            STATE_SETTINGS_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_settings_value)],
            STATE_MAINT_WINDOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_maintenance_window)],
            STATE_RESOLVE_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_resolve_note)],
            STATE_CSV_UPLOAD:   [MessageHandler(filters.Document.FileExtension("csv"), msg_csv_upload)],
            STATE_EDIT_ITEM_ID: [
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_edit_item_id)
            ],
            STATE_EDIT_ITEM_PHOTO: [
                CallbackQueryHandler(cb_edit_item_photo_keep, pattern=r"^edit_item_photo:keep$"),
                CallbackQueryHandler(cb_item_cancel, pattern=r"^item_add:cancel$"),
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), msg_edit_item_photo)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.COMMAND, cmd_cancel)
        ],
        allow_reentry=True,
        per_message=False,
    )

    # ── Standalone command handlers ────────────────────────────────────────
    app.add_handler(user_conv)
    app.add_handler(admin_conv)

    # Non-conversation commands
    for cmd, handler in [
        ("help",            cmd_help),
        ("orders",          cmd_orders),
        ("profile",         cmd_profile),
        ("addresses",       cmd_addresses),
        ("points",          cmd_points),
        ("refer",           cmd_refer),
        ("spending",        cmd_spending),
        ("notifications",   cmd_notifications),
        ("language",        cmd_language),
        ("deleteaccount",   cmd_delete_account),
        ("support",         cmd_support),
        ("allorders",       cmd_all_orders),
        ("warehouses",      cmd_warehouses),
        ("drivers",         cmd_drivers),
        ("promos",          cmd_promos),
        ("removeitem",      cmd_remove_item),
        ("removewh",        cmd_remove_warehouse),
        ("removedriver",    cmd_remove_driver),
        ("removepromo",     cmd_remove_promo),
        ("removeadmin",     cmd_remove_admin),
        ("admins",          cmd_admins),
        ("settings",        cmd_settings),
        ("revenue",         cmd_revenue),
        ("topitems",        cmd_top_items),
        ("ordertrend",      cmd_order_trend),
        ("lowstock",        cmd_low_stock),
        ("audit",           cmd_audit),
        ("maintenance",     cmd_maintenance),
        ("adjustloyalty",   cmd_adjust_loyalty),
        ("cancel_order",    cmd_cancel_order),
        ("report_issue",    cmd_report_issue),
        ("report",          cmd_report_issue),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    # Inline keyboard callbacks not covered by ConversationHandlers
    app.add_handler(CallbackQueryHandler(cb_cancel,             pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(cb_catalog_page,       pattern=r"^cat_page:"))
    app.add_handler(CallbackQueryHandler(cb_category_select,    pattern=r"^cat_select:"))
    app.add_handler(CallbackQueryHandler(cb_item_detail,        pattern=r"^item_detail:"))
    app.add_handler(CallbackQueryHandler(cb_add_to_cart,        pattern=r"^add_to_cart:"))
    app.add_handler(CallbackQueryHandler(cb_stock_notify,       pattern=r"^stock_notify:"))
    app.add_handler(CallbackQueryHandler(cb_qty_plus,           pattern=r"^qty_plus:"))
    app.add_handler(CallbackQueryHandler(cb_qty_minus,          pattern=r"^qty_minus:"))
    app.add_handler(CallbackQueryHandler(cb_remove_item,        pattern=r"^cart_remove:"))
    app.add_handler(CallbackQueryHandler(cb_clear_cart,         pattern=r"^cart_clear$"))
    app.add_handler(CallbackQueryHandler(cb_checkout,           pattern=r"^checkout$"))
    app.add_handler(CallbackQueryHandler(cb_order_detail,       pattern=r"^order_detail:"))
    app.add_handler(CallbackQueryHandler(cb_reorder,            pattern=r"^reorder:"))
    app.add_handler(CallbackQueryHandler(cb_confirm_cancel,     pattern=r"^confirm_cancel:"))
    app.add_handler(CallbackQueryHandler(cb_issue_type,         pattern=r"^issue_type:"))
    app.add_handler(CallbackQueryHandler(cb_address_default,    pattern=r"^addr_default:"))
    app.add_handler(CallbackQueryHandler(cb_address_delete,     pattern=r"^addr_delete:"))
    app.add_handler(CallbackQueryHandler(cb_loyalty_view,       pattern=r"^loyalty_view$"))
    app.add_handler(CallbackQueryHandler(cb_notif_toggle,       pattern=r"^notif_toggle:"))
    app.add_handler(CallbackQueryHandler(cb_language_select,    pattern=r"^lang_select:"))
    app.add_handler(CallbackQueryHandler(cb_confirm_delete,     pattern=r"^confirm_delete$"))
    app.add_handler(CallbackQueryHandler(cb_admin_menu,         pattern=r"^admin_menu:"))
    app.add_handler(CallbackQueryHandler(cb_admin_order_detail, pattern=r"^admin_order:"))
    app.add_handler(CallbackQueryHandler(cb_admin_update_status,pattern=r"^admin_status:"))
    app.add_handler(CallbackQueryHandler(cb_admin_assign_driver,pattern=r"^admin_assign:"))
    app.add_handler(CallbackQueryHandler(cb_resolve_issue,      pattern=r"^resolve_issue:"))
    app.add_handler(CallbackQueryHandler(cb_do_assign,          pattern=r"^do_assign:"))
    app.add_handler(CallbackQueryHandler(cb_do_resolve,         pattern=r"^do_resolve:"))
    app.add_handler(CallbackQueryHandler(cb_issue_submit,       pattern=r"^issue_submit:"))
    app.add_handler(CallbackQueryHandler(cb_admin_op,           pattern=r"^admin_op:"))
    app.add_handler(CallbackQueryHandler(cb_admin_sf,           pattern=r"^admin_sf:"))
    app.add_handler(CallbackQueryHandler(cb_orders_page,        pattern=r"^orders_page:"))
    app.add_handler(CallbackQueryHandler(cb_orders_list,        pattern=r"^orders_list$"))
    # Overhauled UI button callbacks
    app.add_handler(CallbackQueryHandler(cb_help,               pattern=r"^help$"))
    app.add_handler(CallbackQueryHandler(cb_wh_remove,          pattern=r"^wh_remove:"))
    app.add_handler(CallbackQueryHandler(cb_wh_add_start,       pattern=r"^wh_add:start$"))
    app.add_handler(CallbackQueryHandler(cb_driver_remove,      pattern=r"^driver_remove:"))
    app.add_handler(CallbackQueryHandler(cb_driver_add_start,   pattern=r"^driver_add:start$"))
    app.add_handler(CallbackQueryHandler(cb_driver_add_vehicle, pattern=r"^driver_veh:"))
    app.add_handler(CallbackQueryHandler(cb_admin_remove,       pattern=r"^admin_remove:"))
    app.add_handler(CallbackQueryHandler(cb_admin_add_start,    pattern=r"^admin_add:start$"))
    app.add_handler(CallbackQueryHandler(cb_admin_add_role,     pattern=r"^add_admin_role:"))
    app.add_handler(CallbackQueryHandler(cb_promo_remove,       pattern=r"^promo_remove:"))
    app.add_handler(CallbackQueryHandler(cb_promo_add_start,    pattern=r"^promo_add:start$"))
    app.add_handler(CallbackQueryHandler(cb_promo_flow_type,    pattern=r"^promo_flow_type:"))
    app.add_handler(CallbackQueryHandler(cb_set_setting_key,    pattern=r"^set_key:"))

    # Error handler
    app.add_error_handler(error_handler)

    return app


# ── Lifecycle hooks ────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """
    BUG-001 fix: gRPC channels are created here, inside the running asyncio loop.
    """
    await pool.start()
    logger.info("gRPC channel pool initialised")

    # Start notification polling loop
    app.bot_data["poll_task"] = asyncio.create_task(poll_notifications(app))


async def post_shutdown(app: Application) -> None:
    """
    BUG-003 fix: flush all pending carts before the bot exits.
    """
    cart_manager: CartManager = app.bot_data.get("cart_manager")
    if cart_manager:
        logger.info("flushing pending carts before shutdown…")
        await cart_manager.flush_all()
        logger.info("cart flush complete")

    task = app.bot_data.get("poll_task")
    if task:
        task.cancel()

    await pool.stop()


# ── Error handler ──────────────────────────────────────────────────────────────

async def error_handler(update: object, context) -> None:
    import traceback
    logger.error(
        "unhandled exception:\n%s",
        "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__)),
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            err = context.error
            if isinstance(err, RuntimeError) and "circuit breaker" in str(err).lower():
                msg = "🔌 The backend service is temporarily offline. Please try again in a minute."
            else:
                msg = "⚠️ Something went wrong on our end. Please try again in a moment."
            await update.effective_message.reply_text(msg)
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    app = build_application()

    # ── Datadog Telegram Update Tracing ──────────────────────────────────────
    try:
        from ddtrace import tracer
        original_process_update = app.process_update

        async def traced_process_update(update: Update) -> None:
            user = update.effective_user
            user_id = user.id if user else 0

            # Determine request resource name (command or callback query data)
            resource = "update"
            if update.message and update.message.text:
                resource = update.message.text.split()[0]
            elif update.callback_query and update.callback_query.data:
                resource = f"cb:{update.callback_query.data.split(':')[0]}"

            try:
                with tracer.trace("telegram.update", resource=resource) as span:
                    span.set_tag("telegram.user_id", user_id)
                    if update.message and update.message.text:
                        span.set_tag("telegram.message_length", len(update.message.text))
                    if update.callback_query:
                        span.set_tag("telegram.callback_data", update.callback_query.data)

                    await original_process_update(update)
            except Exception as exc:
                span = tracer.current_span()
                if span:
                    span.set_exc_info(type(exc), exc, exc.__traceback__)
                raise

        app.process_update = traced_process_update
    except ImportError:
        pass

    # BUG-003: SIGTERM is handled by Application.run_polling's shutdown path
    # which calls post_shutdown → cart flush. No manual signal handler needed.
    logger.info("bot starting")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
