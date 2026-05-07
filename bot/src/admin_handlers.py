import logging
import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.ext import ContextTypes, ConversationHandler
from grpc_client import engine
from api import logistics_pb2

# Added local app-level logger trace tracking back container issues easily
logger = logging.getLogger(__name__)

# Admin Conversation States
ADMIN_ADD_NAME, ADMIN_ADD_PRICE, ADMIN_ADD_VOL, ADMIN_ADD_CAT, ADMIN_ADD_STOCK = range(10, 15)

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Pushed blocking gRPC connection onto async's safe to_thread layout
        health = await asyncio.to_thread(engine.check_health)
        status_text = 'Offline — Maintenance' if health.maintenance_mode else 'Online'
        
        # Converted into purely standardized V1 native without invalid Python text escape bugs 
        text = (
            f"👑 *System Administration Panel*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 *System Status:* {status_text}\n"
            f"⚙️ *Engine Version:* {health.version}\n"
        )
        
        kb = [[InlineKeyboardButton("📦 View Catalog", callback_data='admin_view_cat')],[InlineKeyboardButton("➕ Add New Item", callback_data='admin_add_item')],
            [InlineKeyboardButton("🔄 Refresh", callback_data='admin_dash')]
        ]
        
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"Dashboard load failed - GRPC fault context details: {e}")
        # Always prioritize effective target mapping via .effective_message correctly bypassing object nonetype overrides
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Core engine offline.")


# ==========================================
# WIZARD: ADD ITEM
# ==========================================
async def wizard_start_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['new_item'] = {}
    await query.message.reply_text("➕ *Add Item*\nStep 1: Enter product name.", parse_mode='Markdown')
    return ADMIN_ADD_NAME


async def wizard_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'new_item' not in context.user_data: # Fault protection catch resolving session resets immediately!
        context.user_data['new_item'] = {} 
        
    context.user_data['new_item']['name'] = update.message.text
    await update.message.reply_text("Step 2: Enter unit price (e.g. 2500.50):")
    return ADMIN_ADD_PRICE


async def wizard_receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['new_item']['price'] = float(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Numeric value required. Try again:")
        return ADMIN_ADD_PRICE # CRITICAL: explicitly force conversation machine states handling backwards accurately.

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
    
    # Validation checkpoint for random dict crashes cleanly.
    if not item_data: 
        await update.message.reply_text("⚠️ Storage Context dict lost! Let's start over please!")
        return ConversationHandler.END

    proto_item = logistics_pb2.Item(
        id="", # Go engine generates the ID natively
        name=item_data.get('name', 'Unknown'),
        price=item_data.get('price', 0.0),
        volume_cubes=item_data.get('vol', 0.0),
        category=item_data.get('cat', 'Uncategorized'),
        stock=stock
    )
    
    try:
        # To-Thread offloads synchronous GRPC blocking without stalling loop thread!
        res = await asyncio.to_thread(engine.add_item, proto_item)
        if res.success:
            await update.message.reply_text(f"✅ Item saved to database successfully.\nEngine response: {res.message}")
        else:
            await update.message.reply_text(f"⚠️ Engine rejected insert: {res.message}")
    except Exception as e:
        logger.error(f"Failed handling engine mapping insert procedures context dump:\n{e}")
        await update.message.reply_text("⚠️ Failed to reach database socket via Engine router node module logic mapping parameters offline state!")
    finally:    
        # Ensure cleanup resolves securely without leaving ghosts trailing parameters around dict map setups if error throws heavily prior handling. 
        context.user_data.pop('new_item', None)
        
    return ConversationHandler.END
