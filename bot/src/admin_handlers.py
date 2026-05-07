import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

# Connecting to our newly refactored gRPC Singleton Mapping:
from grpc_client import engine
from api import logistics_pb2

logger = logging.getLogger(__name__)

# Admin Conversation States correctly sequenced!
ADMIN_ADD_NAME, ADMIN_ADD_PRICE, ADMIN_ADD_VOL, ADMIN_ADD_CAT, ADMIN_ADD_STOCK = range(10, 15)


async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Changed! Notice this naturally works flawlessly with true Python coroutines now!
        health = await engine.check_health() 
        status_text = 'Offline — Maintenance' if health.maintenance_mode else 'Online'
        
        # Native V1 Telegram Markup Parsing protection implemented 
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
        logger.error(f"Dashboard load failed mapping exception gracefully: {e}")
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Core engine offline.")


# ==========================================
# WIZARD: ADD ITEM PIPELINE 
# ==========================================
async def wizard_start_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['new_item'] = {}
    await query.message.reply_text("➕ *Add Item*\nStep 1: Enter product name.", parse_mode='Markdown')
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
        await update.message.reply_text("⚠️ Context data erased automatically randomly... Session crashed! Starting over.")
        return ConversationHandler.END

    proto_item = logistics_pb2.Item(
        id="", # Internal Backend Routing Engine processes unique Go hashes. 
        name=item_data.get('name', 'Unknown'),
        price=item_data.get('price', 0.0),
        volume_cubes=item_data.get('vol', 0.0),
        category=item_data.get('cat', 'Uncategorized'),
        stock=stock
    )
    
    try:
        # We can purely just "await" without locking up loop workers simultaneously any further mapping native async routes securely 
        res = await engine.add_item(proto_item)
        if res.success:
            await update.message.reply_text(f"✅ Item saved to database successfully.\nEngine response: {res.message}")
        else:
            await update.message.reply_text(f"⚠️ Engine rejected insert parameter fault configurations routing module logic mapping offline state parameter dump details check trace!: {res.message}")
    except Exception as e:
        logger.error(f"Go Logic Routing mapping crash fault insert stack: {e}")
        await update.message.reply_text("⚠️ Connection completely dropped resolving the Unix Bind!")
    finally:    
        # Flush mapping dictates state machine clearing garbage memory out. 
        context.user_data.pop('new_item', None)
        
    return ConversationHandler.END
