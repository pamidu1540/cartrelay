import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler

from grpc_client import engine

logger = logging.getLogger(__name__)

USER_SHOP, AWAIT_LOCATION, CONFIRM_CHECKOUT = range(50, 53)

async def user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entrypoint for the Public Customer Store."""
    if 'cart' not in context.user_data:
        context.user_data['cart'] = {}
        
    try:
        catalog = await engine.get_catalog()
        
        text = f"🛒 *Public Logistics Hub*\nTap below to explore shipping elements."
        
        kb = [[InlineKeyboardButton("➕ Add Sample Box", callback_data="buy_demo_item")],
            [InlineKeyboardButton("📦 Checkout Cart", callback_data="cart_checkout")]
        ]
        
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            
        return USER_SHOP

    except Exception as e:
        logger.error(f"Catalog Error: {e}")
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Engine maintenance.")
        return ConversationHandler.END


async def handle_buy_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Added item!")
    
    cart = context.user_data.get('cart', {})
    cart['DUMMY_101'] = {'qty': cart.get('DUMMY_101', {}).get('qty', 0) + 1}
    context.user_data['cart'] = cart
    return USER_SHOP


async def prompt_checkout_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Secures accurate checkout via Native location request pins."""
    query = update.callback_query
    await query.answer()
    
    if not context.user_data.get('cart'):
        await query.message.reply_text("⚠️ No Items to Checkout!")
        return USER_SHOP

    loc_btn = KeyboardButton(text="📍 Send Location", request_location=True)
    markup = ReplyKeyboardMarkup([[loc_btn]], resize_keyboard=True, one_time_keyboard=True)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text="📡 *Engine Routing Data* \nShare location natively below to calculate shipping:",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    return AWAIT_LOCATION


async def calculate_live_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Offloads precise geographical matrix checkouts to gRPC"""
    location = update.message.location
    lat, lon = location.latitude, location.longitude
    
    msg = await update.message.reply_text("⏳ Processing transport routing...", reply_markup=ReplyKeyboardRemove())

    try:
        quote = await engine.calculate_quote(lat, lon, context.user_data['cart'])
        
        context.user_data['cached_quote'] = quote.SerializeToString()
        context.user_data['cached_total'] = quote.total_price
        
        text = (
            f"✅ *Quote Calculated*\n\n"
            f"Routing Cost: `${quote.routing_fee}`\n"
            f"💎 *Final Checkout:* `${quote.total_price}`\n"
        )
        
        kb = [[InlineKeyboardButton("💳 Finalize Routing", callback_data="finalize_node_purchase")],[InlineKeyboardButton("🚫 Cancel Process", callback_data="cancel_user_checkout")]
        ]
        
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return CONFIRM_CHECKOUT

    except Exception as e:
        logger.error(f"Quoting Engine Timed out: {e}")
        await msg.edit_text("⚠️ Routing Engine Error. Cancellation Complete.")
        context.user_data.clear()
        return ConversationHandler.END


async def submit_place_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The final checkout node finalizing GRPC PlaceOrder pipelines."""
    query = update.callback_query
    await query.answer()

    try:
        resp = await engine.place_order(
            user_id=update.effective_user.id,
            username=update.effective_user.username or "Anonymous",
            quote_data=context.user_data['cached_quote'],
            expected_total=context.user_data['cached_total'],
            receipt_text="Confirmed Node Checkout."
        )
        
        if resp.success:
            await query.edit_message_text(f"🚀 *DISPATCHED SUCCESSFULLY!*\nTracking ID: `{resp.tracking_number}`", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Subsystem Checkout Rejected Transaction.")
             
    except Exception as e:
        logger.error(f"Checkout API disconnected during dispatch request: {e}")
        await query.edit_message_text("⚠️ Order submission failed internally due to Engine Socket disconnecting.")

    context.user_data.clear()
    return ConversationHandler.END


async def user_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standard Memory Cache Exit Routing."""
    context.user_data.clear()
    
    if update.callback_query:
        await update.callback_query.edit_message_text("🚫 Cart Cleared.", reply_markup=None)
    elif update.message:
        await update.message.reply_text("🚫 Route cleared manually.", reply_markup=ReplyKeyboardRemove())
    
    return ConversationHandler.END
