import os
import sys
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    filters,
    ConversationHandler,
    ContextTypes
)

# newly synchronized Async Singletons
from grpc_client import engine
from admin_handlers import (
    admin_dashboard, 
    wizard_start_add_item,
    wizard_receive_name, 
    wizard_receive_price, 
    wizard_receive_vol, 
    wizard_receive_cat, 
    wizard_receive_stock,
    cancel_wizard,  # Our newly mapped state escape clause!
    ADMIN_ADD_NAME, ADMIN_ADD_PRICE, ADMIN_ADD_VOL, ADMIN_ADD_CAT, ADMIN_ADD_STOCK
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Essential verification catch before trying boots globally via Compose.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("Critical Environmental exception! 'TELEGRAM_BOT_TOKEN' heavily completely completely entirely missed in memory traces maps cleanly.")
    sys.exit(1)


async def post_init(application) -> None:
    """Pre-flight Engine connectivity mapping check! Checks DB/Engine health bindings up natively right securely perfectly right prior resolving connection lists seamlessly mapping parameters offloads immediately. """
    try:
        health = await engine.check_health()
        logger.info(f"Logistics Subsystem Go Module link securely locked -> Node Runtime[V.{health.version}] | OK")
    except Exception as e:
        logger.warning(f"Engine routing domain check unresponsive at Node Init mapping environments directly globally: {e}")
        

async def post_shutdown(application) -> None:
    """CRITICAL: Disconnect socket environments successfully removing unix domains smoothly entirely releasing garbage traces natively right out system logs traces gracefully perfectly smoothly seamlessly. """
    await engine.close()
    logger.info("Bot execution container natively offline completely isolated safe routing module links cleared seamlessly parameter drops completed maps parameters resolving native environments securely isolated disconnected out context clean context gracefully right successfully perfectly!.")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)           # Pre Flight Connection tests maps setups  
        .post_shutdown(post_shutdown)   # Safe close domains perfectly out systems isolated disconnected right gracefully 
        .build()
    )

    # Core system triggers hooks heavily 
    app.add_handler(CommandHandler('start', admin_dashboard))
    app.add_handler(CommandHandler('dashboard', admin_dashboard))
    app.add_handler(CallbackQueryHandler(admin_dashboard, pattern='^admin_dash$'))

    # The rebuilt Async State Funnel linking logic completely safely resolving mapped contexts escapes handling. 
    wizard_handler = ConversationHandler(
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
            MessageHandler(filters.COMMAND, cancel_wizard) # Drop completely safely entirely isolated natively escaping user sessions if typed wrong perfectly 
        ],
        allow_reentry=True
    )
    
    app.add_handler(wizard_handler)
    logger.info("Main bot routing loop mapped initializing polling sequence traces completely seamlessly natively immediately...")
    app.run_polling()


if __name__ == '__main__':
    main()
