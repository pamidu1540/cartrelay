import asyncio
import logging
import os
import signal
import sys

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from admin_handlers import (
    ADMIN_ADD_CAT,
    ADMIN_ADD_NAME,
    ADMIN_ADD_PRICE,
    ADMIN_ADD_STOCK,
    ADMIN_ADD_VOL,
    ADMIN_EDIT_STOCK_DELTA,
    ADMIN_EDIT_STOCK_ITEM,
    ADMIN_WH_ADMIN_ID,
    ADMIN_WH_LOC,
    ADMIN_WH_NAME,
    admin_cancel_order,
    admin_dashboard,
    admin_dispatch_order,
    admin_list_warehouses,
    admin_orders_page,
    admin_toggle_maintenance,
    admin_view_catalog,
    admin_view_orders_all,
    admin_view_orders_pending,
    cancel_wizard,
    wizard_receive_cat,
    wizard_receive_name,
    wizard_receive_price,
    wizard_receive_stock,
    wizard_receive_stock_delta,
    wizard_receive_stock_item,
    wizard_receive_vol,
    wizard_receive_wh_admin_id,
    wizard_receive_wh_loc,
    wizard_receive_wh_name,
    wizard_start_add_item,
    wizard_start_add_warehouse,
    wizard_start_edit_stock,
)
from grpc_client import engine
from user_handlers import (
    AWAIT_LOCATION,
    CONFIRM_CHECKOUT,
    USER_SHOP,
    calculate_live_quote,
    handle_buy_item,
    prompt_checkout_location,
    start_cart_flush,
    submit_place_order,
    user_cancel,
    user_start,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN is not set.")
    sys.exit(1)


# ── Lifecycle Hooks ──────────────────────────────────────────────────────────

async def post_init(application) -> None:
    """Initializes async resources after the PTB event loop starts."""
    await engine.init_pool()

    # Start the background task for lazy cart flushing
    await start_cart_flush()

    # Register SIGTERM inside the running loop — avoids the 'Future attached
    # to a different loop' error caused by calling asyncio.get_event_loop()
    # before run_polling() creates its own loop via asyncio.run().
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, application.stop_running)

    try:
        health = await engine.check_health()
        logger.info("engine online — v%s  maintenance=%s", health.version, health.maintenance_mode)
    except Exception as exc:
        logger.warning("engine health check failed at startup: %s", exc)


async def post_shutdown(application) -> None:
    """Catches SIGTERM/SIGINT and gracefully flushes data."""
    logger.info("Shutdown signal received. Flushing pending carts...")

    # [FIX BUG-003]: Flush all pending carts in memory to the Engine
    if hasattr(engine, 'flush_pending_carts'):
        await engine.flush_pending_carts()

    await engine.close()
    logger.info("gRPC channel pool closed.")


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ── User Conversation ─────────────────────────────────────────────────────

    user_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", user_start),
            CallbackQueryHandler(user_start, pattern="^back_to_shop$"),
        ],
        states={
            USER_SHOP: [
                CallbackQueryHandler(handle_buy_item, pattern="^buy_"),
                CallbackQueryHandler(prompt_checkout_location, pattern="^cart_checkout$"),
            ],
            AWAIT_LOCATION: [
                MessageHandler(filters.LOCATION, calculate_live_quote)
            ],
            CONFIRM_CHECKOUT: [
                CallbackQueryHandler(submit_place_order, pattern="^finalize_node_purchase$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", user_cancel),
            CallbackQueryHandler(user_cancel, pattern="^cancel_user_checkout$"),
        ],
        allow_reentry=True,
    )
    app.add_handler(user_conv)

    # ── Admin Panel ───────────────────────────────────────────────────────────

    app.add_handler(CommandHandler("admin", admin_dashboard))
    app.add_handler(CallbackQueryHandler(admin_dashboard,          pattern="^admin_dash$"))
    app.add_handler(CallbackQueryHandler(admin_view_catalog,       pattern="^admin_view_cat$"))
    app.add_handler(CallbackQueryHandler(admin_view_orders_pending,pattern="^admin_view_orders_pending$"))
    app.add_handler(CallbackQueryHandler(admin_view_orders_all,    pattern="^admin_view_orders_all$"))
    app.add_handler(CallbackQueryHandler(admin_orders_page,        pattern="^orders_page_"))
    app.add_handler(CallbackQueryHandler(admin_dispatch_order,     pattern="^dispatch_"))
    app.add_handler(CallbackQueryHandler(admin_cancel_order,       pattern="^cancelord_"))
    app.add_handler(CallbackQueryHandler(admin_toggle_maintenance, pattern="^admin_toggle_maint$"))
    app.add_handler(CallbackQueryHandler(admin_list_warehouses,    pattern="^admin_list_wh$"))

    # [FIX]: Consistent naming wh_wizard for the warehouse wizard
    wh_wizard = ConversationHandler(
        entry_points=[CallbackQueryHandler(wizard_start_add_warehouse, pattern="^admin_add_wh$")],
        states={
            ADMIN_WH_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_wh_name)],
            ADMIN_WH_LOC:      [MessageHandler(filters.LOCATION, wizard_receive_wh_loc)],
            ADMIN_WH_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_wh_admin_id)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_wizard),
            CommandHandler("skip",   wizard_receive_wh_admin_id),
            MessageHandler(filters.COMMAND, cancel_wizard),
        ],
        allow_reentry=True,
    )
    app.add_handler(wh_wizard)

    # Add Item wizard
    add_item_wizard = ConversationHandler(
        entry_points=[CallbackQueryHandler(wizard_start_add_item, pattern="^admin_add_item$")],
        states={
            ADMIN_ADD_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_name)],
            ADMIN_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_price)],
            ADMIN_ADD_VOL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_vol)],
            ADMIN_ADD_CAT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_cat)],
            ADMIN_ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_stock)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_wizard),
            MessageHandler(filters.COMMAND, cancel_wizard),
        ],
        allow_reentry=True,
    )
    app.add_handler(add_item_wizard)

    # Edit Stock wizard
    edit_stock_wizard = ConversationHandler(
        entry_points=[CallbackQueryHandler(wizard_start_edit_stock, pattern="^admin_edit_stock$")],
        states={
            ADMIN_EDIT_STOCK_ITEM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_stock_item)],
            ADMIN_EDIT_STOCK_DELTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_stock_delta)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_wizard),
            MessageHandler(filters.COMMAND, cancel_wizard),
        ],
        allow_reentry=True,
    )
    app.add_handler(edit_stock_wizard)

    logger.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
