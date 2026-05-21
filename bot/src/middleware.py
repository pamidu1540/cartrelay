"""
middleware.py — bot-layer middleware (v5)

Provides:
  • rate_limit()   — per-user token bucket (SEC-005, BUG-005 bot layer)
  • admin_only()   — verifies admin role via engine before executing handler
  • role_required()— finer-grained RBAC decorator
  • check_maintenance() — blocks user commands during maintenance mode
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import defaultdict
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from api import logistics_pb2 as pb
from grpc_client import pool

logger = logging.getLogger(__name__)

# ── Token bucket rate limiter ─────────────────────────────────────────────────
#
# SEC-005 (bot layer): 20 commands per minute per user.
# The engine has its own independent rate limiter as a backstop.

_RATE_CAPACITY = 20
_RATE_REFILL = 20 / 60  # tokens per second

_buckets: dict[int, dict] = defaultdict(lambda: {
    "tokens": float(_RATE_CAPACITY),
    "last": time.monotonic(),
})
_bucket_lock = asyncio.Lock()


async def _check_rate(user_id: int) -> bool:
    async with _bucket_lock:
        b = _buckets[user_id]
        now = time.monotonic()
        elapsed = now - b["last"]
        b["tokens"] = min(_RATE_CAPACITY, b["tokens"] + elapsed * _RATE_REFILL)
        b["last"] = now
        if b["tokens"] >= 1:
            b["tokens"] -= 1
            return True
        return False


def rate_limit(func: Callable) -> Callable:
    """Decorator: silently drops commands when the user exceeds the rate limit."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user and not await _check_rate(user.id):
            logger.warning("rate limit hit: user %d", user.id)
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⏳ You're sending commands too quickly. Please wait a moment."
                )
            return
        return await func(update, context)
    return wrapper


# ── Admin guard ───────────────────────────────────────────────────────────────

def admin_only(func: Callable) -> Callable:
    """Decorator: verifies the caller is an admin before running the handler."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return

        try:
            resp = await pool.call(
                "/logistics.LogisticsEngine/ListAdmins",
                lambda stub, meta: stub.ListAdmins(pb.Empty(), metadata=meta),
                user_id=user.id,
            )
            admin_ids = {a.telegram_id for a in resp.admins}
        except Exception as exc:
            logger.error("admin check failed: %s", exc)
            await update.effective_message.reply_text("⚠️ Could not verify admin status.")
            return

        if user.id not in admin_ids:
            await update.effective_message.reply_text("🚫 Admin access required.")
            return

        return await func(update, context)
    return wrapper


def role_required(*roles: str) -> Callable:
    """Decorator: restricts a handler to admins with one of the specified roles."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if not user:
                return
            try:
                resp = await pool.call(
                    "/logistics.LogisticsEngine/ListAdmins",
                    lambda stub, meta: stub.ListAdmins(pb.Empty(), metadata=meta),
                    user_id=user.id,
                )
                admin_map = {a.telegram_id: a.role for a in resp.admins}
            except Exception:
                await update.effective_message.reply_text("⚠️ Could not verify role.")
                return

            role = admin_map.get(user.id)
            if role not in roles and role != "super_admin":
                await update.effective_message.reply_text(
                    f"🚫 This action requires role: {' or '.join(roles)}"
                )
                return
            context.user_data["_role"] = role
            return await func(update, context)
        return wrapper
    return decorator


# ── Maintenance check ─────────────────────────────────────────────────────────

def check_maintenance(func: Callable) -> Callable:
    """Decorator: blocks user (non-admin) commands during maintenance mode."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            resp = await pool.call(
                "/logistics.LogisticsEngine/CheckHealth",
                lambda stub, meta: stub.CheckHealth(pb.Empty(), metadata=meta),
            )
            if resp.maintenance_mode:
                await update.effective_message.reply_text(
                    "🔧 The service is currently under maintenance. "
                    "We'll be back shortly!"
                )
                return
        except Exception:
            pass  # If we can't reach the engine, let the handler deal with it
        return await func(update, context)
    return wrapper
