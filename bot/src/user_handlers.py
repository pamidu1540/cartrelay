import math
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler
from grpc_client import engine
from api import logistics_pb2

# Conversation States
STATE_BROWSE, STATE_LOCATION, STATE_CONFIRM = range(3)
ITEMS_PER_PAGE = 6

def get_cart_total(cart):
    return sum(item['qty'] * item['price'] for item in cart.values())

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cart', {})

    try:
        health = engine.check_health()
        if health.maintenance_mode:
            text = "😴 The system is currently undergoing maintenance. Please check back later."
            if update.message: await update.message.reply_text(text)
            else: await update.callback_query.edit_message_text(text)
            return ConversationHandler.END
    except Exception as e:
        logging.error(f"Engine connection failed: {e}")
        text = "⚠️ The core engine is currently offline. Please contact support."
        if update.message: await update.message.reply_text(text)
        else: await update.callback_query.edit_message_text(text)
        return ConversationHandler.END

    text = "Welcome to the Logistics Engine v2.0.\nTap below to browse our product catalogue."
    kb = [[InlineKeyboardButton("🛒 Browse Catalog", callback_data='cmd_shop')]]

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    return STATE_BROWSE

async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        response = engine.get_catalog()
    except Exception:
        return await query.edit_message_text("⚠️ Failed to load catalog from engine.")

    if not response.items:
        return await query.edit_message_text("⚠️ The catalogue is currently empty.")

    page = int(query.data.split('page_cat_')[1]) if query.data.startswith('page_cat_') else 0
    categories = list(set([item.category for item in response.items.values()]))
    
    total_pages = math.ceil(len(categories) / ITEMS_PER_PAGE)
    current_cats = categories[page * ITEMS_PER_PAGE: (page + 1) * ITEMS_PER_PAGE]

    kb = [[InlineKeyboardButton(cat, callback_data=f'cat_{cat}')] for cat in current_cats]
    
    nav_row =[]
    if page > 0: nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f'page_cat_{page - 1}'))
    if page < total_pages - 1: nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f'page_cat_{page + 1}'))
    if nav_row: kb.append(nav_row)

    if get_cart_total(context.user_data.get('cart', {})) > 0:
        kb.append([InlineKeyboardButton("🛒 View Cart", callback_data='cmd_cart')])

    await query.edit_message_text("📂 *Select a Category:*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    return STATE_BROWSE

async def show_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith('cat_'):
        context.user_data['current_cat'] = query.data.split('cat_')[1]
        page = 0
    else:
        page = int(query.data.split('page_itm_')[1])

    category = context.user_data.get('current_cat')
    response = engine.get_catalog()
    
    items = [(k, v) for k, v in response.items.items() if v.category == category]
    current_items = items[page * ITEMS_PER_PAGE: (page + 1) * ITEMS_PER_PAGE]

    kb =[]
    for i_id, item in current_items:
        kb.append([InlineKeyboardButton(f"{item.name} - Rs. {item.price:,.2f}", callback_data=f'itm_{i_id}')])

    kb.append([InlineKeyboardButton("🔙 Categories", callback_data='cmd_shop')])
    await query.edit_message_text(f"🛠️ *{category}*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    return STATE_BROWSE

async def start_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()

    cart = context.user_data.get('cart', {})
    if not cart:
        return await query.edit_message_text("Your cart is empty.")

    loc_kb = ReplyKeyboardMarkup([[KeyboardButton("📍 Share Delivery Location", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
    
    if query: await query.delete_message()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="📍 Please share your delivery location.", reply_markup=loc_kb)
    return STATE_LOCATION

async def receive_and_verify_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lon, lat = update.message.location.longitude, update.message.location.latitude
    cart = context.user_data.get('cart', {})
    
    msg = await update.message.reply_text("🔍 Calculating precise routing quotation...", reply_markup=ReplyKeyboardRemove())

    try:
        # Offload 100% of the math, surge pricing, and API calls to the Go Engine
        quote = engine.calculate_quote(lat=lat, lon=lon, cart_dict=cart)
        
        if not quote.success:
            return await msg.edit_text(f"⚠️ {quote.error_message}")
            
        context.user_data['pending_quote'] = quote
        context.user_data['temp_coords'] = {"lat": lat, "lon": lon}

        kb = [[InlineKeyboardButton("✅ Confirm Order", callback_data='cmd_confirm_order')],
            [InlineKeyboardButton("❌ Cancel", callback_data='start_bot')]
        ]
        await msg.edit_text(quote.formatted_receipt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return STATE_CONFIRM

    except Exception as e:
        logging.error(f"Routing error: {e}")
        return await msg.edit_text("⚠️ Engine processing error. Please try again.")

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    quote = context.user_data.get('pending_quote')
    if not quote:
        return await query.edit_message_text("Session expired. Type /start to begin.")

    try:
        # Offload ACID-compliant DB transactions to Go
        res = engine.place_order(
            user_id=update.effective_user.id,
            username=update.effective_user.username or "Unknown",
            quote_data=quote, # Pass the exact quote parameters back
            expected_total=quote.grand_total,
            receipt_text=quote.formatted_receipt
        )
        
        if res.success:
            await query.edit_message_text(f"{quote.formatted_receipt}\n\n*✅ Order Confirmed*\nID: `{res.order_id}`", parse_mode='Markdown')
            context.user_data.pop('cart', None)
        else:
            await query.edit_message_text(f"⚠️ Failed to place order: {res.message}")
            
    except Exception as e:
        await query.edit_message_text("⚠️ Communication error with core database. Please try again.")

    return ConversationHandler.END