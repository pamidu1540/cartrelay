"""
cart_manager.py — cart persistence layer (v5)

Fixes addressed:
  BUG-003: carts are written to the engine immediately on change,
           not buffered in bot memory with a 5 s flush timer.
  BUG-016: no per-user background tasks → no stale tasks to cancel.
  PERF-005: batched flush_all() for shutdown path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING


from api import logistics_pb2 as pb
from grpc_client import pool

logger = logging.getLogger(__name__)


class CartManager:
    """
    Manages cart state for all active users.

    Design decision (v5): carts are persisted to the engine on every
    mutation (add/remove/clear). This eliminates the 5-second in-memory
    buffer that caused data loss on SIGTERM (BUG-003) and the stale
    asyncio tasks that accumulated on /start resets (BUG-016).

    The only in-memory state is a dict of {user_id: [CartItem]} used
    to build the inline keyboard quickly without an extra gRPC round-trip.
    """

    def __init__(self) -> None:
        self._local: dict[int, list[pb.CartItem]] = {}
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def get(self, user_id: int) -> list[pb.CartItem]:
        """Return the user's current cart, loading from engine if not cached."""
        async with self._lock:
            if user_id in self._local:
                return list(self._local[user_id])

        # Load from engine
        try:
            resp = await pool.call(
                "/logistics.LogisticsEngine/GetCart",
                lambda stub, meta: stub.GetCart(
                    pb.UserId(user_id=user_id), metadata=meta
                ),
                user_id=user_id,
            )
            async with self._lock:
                self._local[user_id] = list(resp.items)
            return list(resp.items)
        except Exception as exc:
            logger.warning("load cart failed for user %d: %s", user_id, exc)
            return []

    async def set_items(self, user_id: int, items: list[pb.CartItem]) -> None:
        """Replace the user's cart and persist to engine immediately."""
        async with self._lock:
            old_items = self._local.get(user_id)
            self._local[user_id] = list(items)
        try:
            await self._persist(user_id, items)
        except Exception:
            async with self._lock:
                if old_items is None:
                    self._local.pop(user_id, None)
                else:
                    self._local[user_id] = old_items
            raise

    async def add_item(self, user_id: int, item_id: str, qty: int = 1, note: str = "") -> list[pb.CartItem]:
        """Add qty of item_id to the cart. Returns updated cart."""
        items = await self.get(user_id)
        for ci in items:
            if ci.item_id == item_id:
                ci.quantity += qty
                if ci.quantity <= 0:
                    items = [i for i in items if i.item_id != item_id]
                await self.set_items(user_id, items)
                return items
        if qty > 0:
            items.append(pb.CartItem(item_id=item_id, quantity=qty, note=note))
        await self.set_items(user_id, items)
        return items

    async def remove_item(self, user_id: int, item_id: str) -> list[pb.CartItem]:
        """Remove item_id entirely from the cart."""
        items = await self.get(user_id)
        items = [ci for ci in items if ci.item_id != item_id]
        await self.set_items(user_id, items)
        return items

    async def set_quantity(self, user_id: int, item_id: str, qty: int) -> list[pb.CartItem]:
        """Set the quantity of item_id. qty <= 0 removes the item."""
        items = await self.get(user_id)
        if qty <= 0:
            items = [ci for ci in items if ci.item_id != item_id]
        else:
            for ci in items:
                if ci.item_id == item_id:
                    ci.quantity = qty
                    break
            else:
                items.append(pb.CartItem(item_id=item_id, quantity=qty))
        await self.set_items(user_id, items)
        return items

    async def clear(self, user_id: int) -> None:
        """Clear the user's cart locally and in the engine."""
        async with self._lock:
            self._local.pop(user_id, None)
        try:
            await pool.call(
                "/logistics.LogisticsEngine/ClearCart",
                lambda stub, meta: stub.ClearCart(
                    pb.UserId(user_id=user_id), metadata=meta
                ),
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning("clear cart failed for user %d: %s", user_id, exc)

    async def invalidate_local(self, user_id: int) -> None:
        """
        BUG-016 fix: called when a user resets their conversation state (/start).
        Drops the local cache so the next get() reloads from the engine.
        No background tasks to cancel — there are none.
        """
        async with self._lock:
            self._local.pop(user_id, None)

    # ── Shutdown flush ─────────────────────────────────────────────────────────

    async def flush_all(self) -> None:
        """
        BUG-003 fix: persist all in-memory carts to the engine before shutdown.
        Called from post_shutdown hook in main.py.
        """
        async with self._lock:
            snapshot = dict(self._local)

        if not snapshot:
            return

        tasks = [
            self._persist(uid, items)
            for uid, items in snapshot.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errs = [r for r in results if isinstance(r, Exception)]
        if errs:
            logger.warning("flush_all: %d cart(s) failed to persist", len(errs))
        else:
            logger.info("flush_all: %d cart(s) flushed", len(snapshot))

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _persist(self, user_id: int, items: list[pb.CartItem]) -> None:
        try:
            await pool.call(
                "/logistics.LogisticsEngine/SaveCart",
                lambda stub, meta: stub.SaveCart(
                    pb.SaveCartRequest(user_id=user_id, items=items),
                    metadata=meta,
                ),
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning("persist cart failed for user %d: %s", user_id, exc)
            raise
