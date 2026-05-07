import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from grpc_client import engine
from api import logistics_pb2

logger = logging.getLogger(__name__)

ADMIN_ADD_NAME, ADMIN_ADD_PRICE, ADMIN_ADD_VOL, ADMIN_ADD_CAT, ADMIN_ADD_STOCK = range(10, 15)

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        health = await engine.check_health() 
        status_text = 'Offline — Maintenance' if health.maintenance_mode else 'Online'
        
        text = (
            f"👑 *System Administration Panel*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 *System Status:* {status_text}\n"
            f"⚙️ *Engine Version:* {health.version}\n"
        )
        
        kb = [[InlineKeyboardButton("📦 View Catalog", callback_data='admin_view_cat')],[InlineKeyboardButton("➕ Add New Item", callback_data='admin_add_item')],[InlineKeyboardButton("🔄 Refresh", callback_data='admin_dash')]
        ]
        
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"Dashboard load exception: {e}")
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Core engine offline.")


async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Graceful bailout for the Add Item Wizard"""
    context.user_data.pop('new_item', None)
    
    cancel_text = "🚫 Setup manually canceled."
    kb = [[InlineKeyboardButton("Back to Dashboard", callback_data='admin_dash')]]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(cancel_text, reply_markup=InlineKeyboardMarkup(kb))
    elif update.message:
        await update.message.reply_text(cancel_text, reply_markup=InlineKeyboardMarkup(kb))
    
    return ConversationHandler.END


# ==========================================
# WIZARD: ADD ITEM 
# ==========================================
async def wizard_start_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['new_item'] = {}
    await query.message.reply_text("➕ *Add Item*\nStep 1: Enter product name.\n*(Or type /cancel to stop)*", parse_mode='Markdown')
    return ADMIN_ADD_NAME

async def wizard_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'new_item' not in context.user_data:
        context.user_data['new_item'] = {} 
        
    context.user_data['new_item']['name'] = update.message.text
    await update.message.reply_text("Step 2: Enter unit price (e.g. 2500.50):")
    return ADMIN_ADD_PRICE

async def wizard_receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['new_item']['price'] = float(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Numeric value required. Try again:")
        return ADMIN_ADD_PRICE 

    await update.message.reply_text("Step 3: Enter volume in cubes (e.g. 0.05):")
    return ADMIN_ADD_VOL

async def wizard_receive_vol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: 
        context.user_data['new_item']['vol'] = float(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Numeric value required. Try again:")
        return ADMIN_ADD_VOL
        
    await update.message.reply_text("Step 4: Enter category:")
    return ADMIN_ADD_CAT

async def wizard_receive_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_item']['cat'] = update.message.text
    await update.message.reply_text("Step 5: Enter initial stock:")
    return ADMIN_ADD_STOCK

async def wizard_receive_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stock = int(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Whole number required. Try again:")
        return ADMIN_ADD_STOCK
        
    item_data = context.user_data.get('new_item', {})
    if not item_data: 
        await update.message.reply_text("⚠️ Context lost! Starting over.")
        return ConversationHandler.END

    proto_item = logistics_pb2.Item(
        id="",
        name=item_data.get('name', 'Unknown'),
        price=item_data.get('price', 0.0),
        volume_cubes=item_data.get('vol', 0.0),
        category=item_data.get('cat', 'Uncategorized'),
        stock=stock
    )
    
    try:
        res = await engine.add_item(proto_item)
        if res.success:
            await update.message.reply_text(f"✅ Item saved to database successfully.\nEngine response: {res.message}")
        else:
            await update.message.reply_text(f"⚠️ Engine rejected insert: {res.message}")
    except Exception as e:
        logger.error(f"Engine insert exception: {e}")
        await update.message.reply_text("⚠️ Connection dropped while resolving the Unix Bind!")
    finally:    
        context.user_data.pop('new_item', None)
        
    return ConversationHandler.END
