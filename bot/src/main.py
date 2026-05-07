import os
import sys
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes
)

from grpc_client import engine
from admin_handlers import (
    admin_dashboard, wizard_start_add_item, wizard_receive_name, wizard_receive_price, 
    wizard_receive_vol, wizard_receive_cat, wizard_receive_stock, cancel_wizard, 
    ADMIN_ADD_NAME, ADMIN_ADD_PRICE, ADMIN_ADD_VOL, ADMIN_ADD_CAT, ADMIN_ADD_STOCK
)
from user_handlers import (
    user_start, handle_buy_item, prompt_checkout_location, calculate_live_quote, 
    submit_place_order, user_cancel, USER_SHOP, AWAIT_LOCATION, CONFIRM_CHECKOUT
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN missing in environment!")
    sys.exit(1)


async def post_init(application) -> None:
    try:
        health = await engine.check_health()
        logger.info(f"Node Subsystem Verified! Module Engine Core[V.{health.version}] Status OK.")
    except Exception as e:
        logger.warning(f"Engine checks offline completely: {e}")


async def post_shutdown(application) -> None:
    await engine.close()
    logger.info("Module Disconnected From Routing Socket Domains Entirely Safely.")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # == GLOBAL / PUBLIC Handlers Configuration ========================
    user_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', user_start), CallbackQueryHandler(user_start, pattern='^back_to_shop$')],
        states={
            USER_SHOP:[
                CallbackQueryHandler(handle_buy_item, pattern='^buy_'),
                CallbackQueryHandler(prompt_checkout_location, pattern='^cart_checkout$')
            ],
            AWAIT_LOCATION:[MessageHandler(filters.LOCATION, calculate_live_quote)],
            CONFIRM_CHECKOUT:[CallbackQueryHandler(submit_place_order, pattern='^finalize_node_purchase$')]
        },
        fallbacks=[
            CommandHandler('cancel', user_cancel), 
            CallbackQueryHandler(user_cancel, pattern='^cancel_user_checkout$')
        ],
    )
    
    app.add_handler(user_conv_handler)

    # == ADMIN CONFIGURATION OVERRIDES ==================================
    app.add_handler(CommandHandler('admin', admin_dashboard))
    app.add_handler(CallbackQueryHandler(admin_dashboard, pattern='^admin_dash$'))

    admin_wizard_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(wizard_start_add_item, pattern='^admin_add_item$')],
        states={
            ADMIN_ADD_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_name)],
            ADMIN_ADD_PRICE:[MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_price)],
            ADMIN_ADD_VOL:[MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_vol)],
            ADMIN_ADD_CAT:[MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_cat)],
            ADMIN_ADD_STOCK:[MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_receive_stock)]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_wizard), 
            MessageHandler(filters.COMMAND, cancel_wizard)
        ],
        allow_reentry=True
    )
    app.add_handler(admin_wizard_handler)

    logger.info("Main bot routing initialization parameters loading!")
    app.run_polling()

if __name__ == '__main__':
    main()
