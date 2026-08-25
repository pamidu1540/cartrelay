"""
catalog.py — Admin catalog operations: add item wizard, edit photo/stock/price, remove item, CSV bulk import.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging

import grpc
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from message_manager import delete_user_message, send_or_edit_main_menu

from api import logistics_pb2 as pb
from grpc_client import pool
from middleware import admin_only
from handlers.admin.common import (
    STATE_CSV_UPLOAD,
    STATE_EDIT_ITEM_ID,
    STATE_EDIT_ITEM_PHOTO,
    STATE_ITEM_CATEGORY,
    STATE_ITEM_DESC,
    STATE_ITEM_NAME,
    STATE_ITEM_PHOTO,
    STATE_ITEM_PRICE,
    STATE_ITEM_STOCK,
    _reply,
    _rpc_error,
)

logger = logging.getLogger(__name__)


# ── Catalog Management ───────────────────────────────────────────────────────

async def cb_item_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await send_or_edit_main_menu(update, context, "❌ Operation cancelled.")
    else:
        await _reply(update, context, "❌ Operation cancelled.")
    context.user_data.pop("new_item", None)
    return ConversationHandler.END


@admin_only
async def cmd_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    context.user_data["new_item"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
    await _reply(update, context, "➕ *Add New Item Wizard*\n\nStep 1/6 — Enter the item *name*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_NAME


async def cb_item_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["new_item"] = {}
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
    await send_or_edit_main_menu(update, context, "➕ *Add New Item Wizard*\n\nStep 1/6 — Enter the item *name*:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_NAME


async def msg_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    context.user_data.setdefault("new_item", {})["name"] = update.effective_message.text.strip()
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
    await _reply(update, context, "Step 2/6 — Enter the *price* (LKR, numbers only):", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_PRICE


async def msg_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    try:
        price = float(update.effective_message.text.strip())
        if price < 0:
            raise ValueError("negative price")
    except ValueError:
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
        await _reply(update, context, "❌ Please enter a valid positive number for price:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_ITEM_PRICE
    context.user_data["new_item"]["price"] = price
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
    await _reply(update, context, "Step 3/6 — Enter initial *stock* quantity:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_STOCK


async def msg_item_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    try:
        stock = int(update.effective_message.text.strip())
        if stock < 0:
            raise ValueError("negative stock")
    except ValueError:
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
        await _reply(update, context, "❌ Please enter a valid non-negative integer for stock:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_ITEM_STOCK
    context.user_data["new_item"]["stock"] = stock

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/GetCatalog",
            lambda stub, meta: stub.GetCatalog(
                pb.CatalogRequest(page=0, page_size=200), metadata=meta
            ),
        )
        categories = sorted({it.category for it in resp.items if it.category})
    except Exception:
        categories = []

    kb = []
    for cat in categories[:6]:
        kb.append([InlineKeyboardButton(cat, callback_data=f"item_cat_select:{cat}")])
    kb.append([InlineKeyboardButton("➕ Type New Category", callback_data="item_cat_select:type_new")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")])

    await _reply(update, context, "Step 4/6 — Choose a category below or type a new one:", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_CATEGORY


async def cb_item_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    cat = query.data.split(":", 1)[1]

    if cat == "type_new":
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
        await send_or_edit_main_menu(update, context, "✏️ Please type the new category name:", reply_markup=InlineKeyboardMarkup(kb))
        return STATE_ITEM_CATEGORY

    if len(cat) > 50:
        cat = cat[:50]

    context.user_data.setdefault("new_item", {})["category"] = cat
    await send_or_edit_main_menu(update, context, f"✅ Category set to: *{cat}*")

    kb = [
        [InlineKeyboardButton("⏩ Skip Photo", callback_data="item_photo_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")],
    ]
    await send_or_edit_main_menu(update, context, "Step 5/6 — Send the item *photo* (or tap skip):", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_PHOTO


async def msg_item_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    cat = update.effective_message.text.strip()
    if len(cat) > 50:
        cat = cat[:50]
    context.user_data.setdefault("new_item", {})["category"] = cat
    kb = [
        [InlineKeyboardButton("⏩ Skip Photo", callback_data="item_photo_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")],
    ]
    await _reply(update, context, "Step 5/6 — Send the item *photo* (or tap skip):", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_PHOTO


async def cb_item_photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_or_edit_main_menu(update, context, "⏩ Photo skipped.")

    kb = [
        [InlineKeyboardButton("⏩ Skip Description", callback_data="item_desc_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")],
    ]
    await send_or_edit_main_menu(update, context, "Step 6/6 — Enter the item *description* (or tap skip):", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_DESC


async def msg_item_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    msg = update.effective_message
    if msg.photo:
        context.user_data.setdefault("new_item", {})["photo_file_id"] = msg.photo[-1].file_id
    elif msg.text and msg.text.lower() != "skip":
        pass

    kb = [
        [InlineKeyboardButton("⏩ Skip Description", callback_data="item_desc_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")],
    ]
    await _reply(update, context, "Step 6/6 — Enter the item *description* (or tap skip):", reply_markup=InlineKeyboardMarkup(kb))
    return STATE_ITEM_DESC


async def cb_item_desc_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_or_edit_main_menu(update, context, "⏩ Description skipped.")

    d = context.user_data.pop("new_item", {})
    item = pb.Item(
        name=d.get("name", ""),
        price=d.get("price", 0.0),
        stock=d.get("stock", 0),
        category=d.get("category", "Uncategorized"),
        photo_file_id=d.get("photo_file_id", ""),
        description="",
        is_available=True,
        tax_rate=-1,
    )
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddItem",
            lambda stub, meta: stub.AddItem(item, metadata=meta),
            user_id=update.effective_user.id,
        )
        await send_or_edit_main_menu(update, context, f"✅ *{item.name}* added successfully! ID: `{resp.entity_id}`", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await send_or_edit_main_menu(update, context, f"❌ Failed to add item: {e}")
    return ConversationHandler.END


async def msg_item_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    text = update.effective_message.text.strip()
    if text.lower() != "skip":
        context.user_data.setdefault("new_item", {})["description"] = text

    d = context.user_data.pop("new_item", {})
    item = pb.Item(
        name=d.get("name", ""),
        price=d.get("price", 0.0),
        stock=d.get("stock", 0),
        category=d.get("category", "Uncategorized"),
        photo_file_id=d.get("photo_file_id", ""),
        description=d.get("description", ""),
        is_available=True,
        tax_rate=-1,
    )
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/AddItem",
            lambda stub, meta: stub.AddItem(item, metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, f"✅ *{item.name}* added! ID: `{resp.entity_id}`")
    except Exception as e:
        await _reply(update, context, f"❌ Failed to add item: {e}")
    return ConversationHandler.END


@admin_only
async def cmd_edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
    await _reply(update, context,
        "✏️ *Edit Item Wizard*\n\nPlease enter the **item ID** to edit (get IDs from /catalog):",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_EDIT_ITEM_ID


@admin_only
async def msg_edit_item_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    item_id = update.effective_message.text.strip()
    try:
        item = await pool.call(
            "/logistics.LogisticsEngine/GetItem",
            lambda stub, meta: stub.GetItem(pb.ItemId(id=item_id), metadata=meta),
        )
    except Exception as e:
        kb = [[InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")]]
        await _reply(update, context,
            f"❌ Item not found or engine error: {e}\n\nPlease enter a valid **item ID**:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return STATE_EDIT_ITEM_ID

    context.user_data["edit_item"] = item

    kb = [
        [InlineKeyboardButton("⏩ Keep Current Photo", callback_data="edit_item_photo:keep")],
        [InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")],
    ]
    await _reply(update, context,
        f"🔍 *Found Item:* {item.name}\n"
        f"💰 *Current Price:* LKR {item.price:.2f}\n"
        f"📦 *Current Stock:* {item.stock}\n\n"
        "📸 Please send a **new photo** for this item, or click **Keep Current Photo**:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return STATE_EDIT_ITEM_PHOTO


@admin_only
async def msg_edit_item_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    msg = update.effective_message
    item = context.user_data.pop("edit_item", None)
    if not item:
        await _reply(update, context, "❌ Edit session expired. Please start over with /edititem.")
        return ConversationHandler.END

    if msg.photo:
        photo_file_id = msg.photo[-1].file_id
        item.photo_file_id = photo_file_id
    else:
        kb = [
            [InlineKeyboardButton("⏩ Keep Current Photo", callback_data="edit_item_photo:keep")],
            [InlineKeyboardButton("❌ Cancel", callback_data="item_add:cancel")],
        ]
        context.user_data["edit_item"] = item
        await _reply(update, context,
            "❌ Invalid input. Please upload a photo, or click the **Keep Current Photo** button below:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return STATE_EDIT_ITEM_PHOTO

    try:
        await pool.call(
            "/logistics.LogisticsEngine/EditItem",
            lambda stub, meta: stub.EditItem(item, metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, f"✅ Photo for *{item.name}* updated successfully!")
    except Exception as e:
        await _reply(update, context, f"❌ Failed to edit item photo: {e}")

    return ConversationHandler.END


@admin_only
async def cb_edit_item_photo_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    item = context.user_data.pop("edit_item", None)
    if not item:
        await send_or_edit_main_menu(update, context, "❌ Edit session expired. Please start over with /edititem.")
        return ConversationHandler.END

    await send_or_edit_main_menu(update, context, f"⏩ Current photo for *{item.name}* kept. No changes made.")
    return ConversationHandler.END


@admin_only
async def cmd_remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await _reply(update, context, "Usage: `/removeitem <item_id>`")
        return
    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/RemoveItem",
            lambda stub, meta: stub.RemoveItem(pb.ItemId(id=args[0]), metadata=meta),
            user_id=update.effective_user.id,
        )
        await _reply(update, context, "✅ Item removed." if resp.success else f"❌ {resp.message}")
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)


@admin_only
async def cmd_bulk_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    await _reply(update, context,
        "📥 *Bulk Import Items*\n\n"
        "Upload a CSV with columns:\n"
        "`name, price, stock, category, description`\n\n"
        "Send the CSV file now:",
    )
    return STATE_CSV_UPLOAD


async def cb_bulk_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📥 *Bulk Import Items*\n\n"
        "Upload a CSV with columns:\n"
        "`name, price, stock, category, description`\n\n"
        "Send the CSV file now:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return STATE_CSV_UPLOAD


async def msg_csv_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await delete_user_message(update, context)
    doc = update.effective_message.document
    if not doc:
        await _reply(update, context, "❌ Please send a CSV file.")
        return STATE_CSV_UPLOAD

    f = await doc.get_file()
    raw = await f.download_as_bytearray()

    def _parse_csv(raw_bytes: bytearray) -> list[pb.Item]:
        reader = csv.DictReader(io.StringIO(raw_bytes.decode("utf-8", errors="replace")))
        items_list: list[pb.Item] = []
        for row in reader:
            try:
                items_list.append(pb.Item(
                    name=row.get("name", "").strip(),
                    price=float(row.get("price", 0)),
                    stock=int(row.get("stock", 0)),
                    category=row.get("category", "Uncategorized").strip(),
                    description=row.get("description", "").strip(),
                    is_available=True,
                    tax_rate=-1,
                ))
            except (ValueError, KeyError) as exc:
                logger.warning("CSV row skip: %s", exc)
        return items_list

    items = await asyncio.to_thread(_parse_csv, raw)

    if not items:
        await _reply(update, context, "❌ No valid rows found in CSV.")
        return ConversationHandler.END

    try:
        resp = await pool.call(
            "/logistics.LogisticsEngine/BulkImportItems",
            lambda stub, meta: stub.BulkImportItems(
                pb.BulkImportRequest(items=items), metadata=meta
            ),
            user_id=update.effective_user.id,
        )
        msg = f"✅ Import done — created: {resp.created}, updated: {resp.updated}"
        if resp.errors:
            msg += f"\nErrors ({len(resp.errors)}):\n" + "\n".join(resp.errors[:5])
        await _reply(update, context, msg)
    except grpc.RpcError as e:
        await _rpc_error(update, context, e)
    return ConversationHandler.END
