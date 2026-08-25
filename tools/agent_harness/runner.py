"""
runner.py — Lifecycle manager and Dispatcher for the Bot Application within the Agent Test Harness.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

# Ensure bot/src is on the Python path
BOT_SRC = Path(__file__).resolve().parent.parent.parent / "bot" / "src"
if str(BOT_SRC) not in sys.path:
    sys.path.insert(0, str(BOT_SRC))

# Set test environment defaults if not provided
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:MOCK_TOKEN_FOR_AGENT_HARNESS")
os.environ.setdefault("ENGINE_GRPC_ADDR", "127.0.0.1:50051")
os.environ.setdefault("BOT_HMAC_SECRET", "dev-secret-key-32-chars-minimum!!")

from telegram.ext import Application  # noqa: E402

import main as bot_main  # noqa: E402
from api import logistics_pb2 as pb  # noqa: E402
from grpc_client import pool  # noqa: E402
from tools.agent_harness.mock_bot import MockTelegramBot, ScreenState, make_user_update, tracker  # noqa: E402

logger = logging.getLogger("agent_harness.runner")


class AgentTestHarness:
    """Manages the in-memory Bot Application and provides direct test dispatching."""

    def __init__(self) -> None:
        self.bot: MockTelegramBot = MockTelegramBot()
        self.app: Application | None = None
        self._is_running: bool = False

    async def start(self) -> None:
        """Initialises the bot application, handlers, and gRPC connection pool."""
        if self._is_running:
            return

        logger.info("Initializing Agent Test Harness application...")
        # Build standard bot application with custom mock bot
        self.app = bot_main.build_application(bot=self.bot)

        # Run application lifecycle initialization
        await self.app.initialize()
        await self.app.start()
        await bot_main.post_init(self.app)

        self._is_running = True
        logger.info("Agent Test Harness running and ready for simulation.")

    async def stop(self) -> None:
        """Flushes in-memory carts and shuts down channels."""
        if not self._is_running or not self.app:
            return

        logger.info("Shutting down Agent Test Harness...")
        await bot_main.post_shutdown(self.app)
        await self.app.stop()
        await self.app.shutdown()
        self._is_running = False
        logger.info("Agent Test Harness stopped.")

    async def send_text(
        self,
        user_id: int,
        text: str,
        username: str = "agent_tester",
        first_name: str = "Agent",
    ) -> ScreenState:
        """Simulates the user typing a command or text message."""
        if not self._is_running:
            await self.start()

        update, _ = make_user_update(
            user_id=user_id,
            text=text,
            username=username,
            first_name=first_name,
            bot=self.bot,
        )
        await self.app.process_update(update)
        return tracker.get_screen(user_id)

    async def click_button(
        self,
        user_id: int,
        callback_data: str,
        username: str = "agent_tester",
    ) -> ScreenState:
        """Simulates the user clicking an inline keyboard button."""
        if not self._is_running:
            await self.start()

        update, _ = make_user_update(
            user_id=user_id,
            callback_data=callback_data,
            username=username,
            bot=self.bot,
        )
        await self.app.process_update(update)
        return tracker.get_screen(user_id)

    async def share_location(
        self,
        user_id: int,
        latitude: float,
        longitude: float,
        username: str = "agent_tester",
    ) -> ScreenState:
        """Simulates the user sharing a GPS location."""
        if not self._is_running:
            await self.start()

        update, _ = make_user_update(
            user_id=user_id,
            latitude=latitude,
            longitude=longitude,
            username=username,
            bot=self.bot,
        )
        await self.app.process_update(update)
        return tracker.get_screen(user_id)

    def get_screen(self, user_id: int) -> ScreenState:
        """Gets the user's active screen state."""
        return tracker.get_screen(user_id)

    def get_transcript(self, user_id: int) -> list[dict[str, Any]]:
        """Gets the full chronological chat transcript."""
        return [r.to_dict() for r in tracker.get_transcript(user_id)]

    async def reset_session(self, user_id: int | None = None) -> None:
        """Resets chat screens, messages, and in-memory cart cache."""
        tracker.reset(user_id)
        if self.app:
            cart_manager = self.app.bot_data.get("cart_manager")
            if cart_manager and user_id is not None:
                await cart_manager.invalidate_local(user_id)

    # ── Direct Backend Inspection Methods ────────────────────────────────────

    async def inspect_cart(self, user_id: int) -> list[dict[str, Any]]:
        """Directly inspects the active in-memory / persisted cart state for a user."""
        if not self.app:
            return []
        cart_manager = self.app.bot_data.get("cart_manager")
        if not cart_manager:
            return []
        items = await cart_manager.get_cart(user_id)
        return [
            {
                "item_id": it.item_id,
                "quantity": it.quantity,
                "note": getattr(it, "note", ""),
            }
            for it in items
        ]

    async def inspect_order(self, order_id: str, actor_id: int = 0) -> dict[str, Any]:
        """Queries the Go Logistics Engine gRPC service to fetch an order."""
        try:
            resp = await pool.call(
                "/logistics.LogisticsEngine/GetOrder",
                lambda stub, meta: stub.GetOrder(pb.OrderId(order_id=order_id), metadata=meta),
                user_id=actor_id,
            )
            return {
                "order_id": resp.order_id,
                "human_ref": resp.human_ref,
                "status": resp.status,
                "user_id": resp.user_id,
                "total": resp.total,
                "delivery_address": resp.delivery_address,
                "driver_name": resp.driver_name,
                "items": [{"item_id": it.item_id, "quantity": it.quantity, "unit_price": it.unit_price} for it in resp.items],
            }
        except Exception as e:
            return {"error": str(e)}

    async def inspect_catalog(self, page: int = 0, page_size: int = 50) -> list[dict[str, Any]]:
        """Queries the Go Logistics Engine gRPC service to fetch catalog items."""
        try:
            resp = await pool.call(
                "/logistics.LogisticsEngine/GetCatalog",
                lambda stub, meta: stub.GetCatalog(pb.CatalogRequest(page=page, page_size=page_size), metadata=meta),
            )
            return [
                {
                    "id": it.id,
                    "name": it.name,
                    "price": it.price,
                    "stock": it.stock,
                    "category": it.category,
                    "is_available": it.is_available,
                }
                for it in resp.items
            ]
        except Exception as e:
            return [{"error": str(e)}]


# Global harness instance
harness = AgentTestHarness()
