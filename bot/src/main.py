import os
import logging
from telegram.ext import ApplicationBuilder, CommandHandler, ConversationHandler, CallbackQueryHandler, MessageHandler, filters
from user_handlers import start_bot, show_categories, receive_and_verify_location, STATE_BROWSE, STATE_LOCATION, STATE_CONFIRM

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    shopping_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_bot),
            CallbackQueryHandler(start_bot, pattern='^start_bot$')
        ],
        states={
            STATE_BROWSE:[
                CallbackQueryHandler(show_categories, pattern='^cmd_shop$')
                # Include other item selection handlers here
            ],
            STATE_LOCATION:[
                MessageHandler(filters.LOCATION, receive_and_verify_location)
            ],
            STATE_CONFIRM: [
                # Include confirm order handler here
            ]
        },
        fallbacks=[CommandHandler("start", start_bot)],
    )

    app.add_handler(shopping_conv)
    logging.info("Bot UI Layer started successfully.")
    app.run_polling()