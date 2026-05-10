"""
gRPC client for the logistics engine.

FIX: Channel pool (8 channels, round-robin) replaces a single channel so
concurrent Telegram update handlers don't queue on one connection.
"""

from itertools import cycle
import logging
import os
import sys
import uuid

import grpc
from api import logistics_pb2, logistics_pb2_grpc

logger = logging.getLogger(__name__)

SOCKET_PATH = "unix:///app/sockets/logistics.sock"

INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN")
if not INTERNAL_TOKEN:
    logger.critical("INTERNAL_TOKEN not set — engine auth will fail on every call.")
    sys.exit(1)

# FIX: Pool of 8 channels so concurrent handlers don't queue behind each other.
_POOL_SIZE = 8

# Channel options for all connections in the pool.
_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms",              10_000),   # ping every 10 s
    ("grpc.keepalive_timeout_ms",            3_000),   # 3 s to acknowledge
    ("grpc.keepalive_permit_without_calls",      1),   # ping even when idle
    ("grpc.http2.max_pings_without_data",        0),   # no limit
    ("grpc.http2.min_time_between_pings_ms",  5_000),
]

_channels = [
    grpc.aio.insecure_channel(SOCKET_PATH, options=_CHANNEL_OPTIONS)
    for _ in range(_POOL_SIZE)
]
_pool = cycle(_channels)


def _get_channel() -> grpc.aio.Channel:
    return next(_pool)


def _meta(request_id: str | None = None) -> tuple:
    """Return gRPC call metadata with auth token and correlation id."""
    return (
        ("x-internal-token", INTERNAL_TOKEN),
        ("x-request-id", request_id or str(uuid.uuid4())),
    )


class EngineClient:
    """Thin async wrapper around the LogisticsEngine gRPC stub."""

    DEFAULT_TIMEOUT = 8.0

    def _stub(self) -> logistics_pb2_grpc.LogisticsEngineStub:
        return logistics_pb2_grpc.LogisticsEngineStub(_get_channel())

    # ── Health ────────────────────────────────────────────────────────────────

    async def check_health(self, request_id: str | None = None):
        return await self._stub().CheckHealth(
            logistics_pb2.Empty(),
            metadata=_meta(request_id),
            timeout=3.0,
        )

    async def toggle_maintenance(self, request_id: str | None = None):
        return await self._stub().ToggleMaintenance(
            logistics_pb2.Empty(),
            metadata=_meta(request_id),
            timeout=5.0,
        )

    # ── Catalog ───────────────────────────────────────────────────────────────

    async def get_catalog(self, request_id: str | None = None):
        return await self._stub().GetCatalog(
            logistics_pb2.Empty(),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def add_item(self, item_proto, request_id: str | None = None):
        return await self._stub().AddItem(
            item_proto,
            metadata=_meta(request_id),
            timeout=10.0,
        )

    async def edit_item_price(
        self, item_id: str, new_price: float, request_id: str | None = None
    ):
        return await self._stub().EditItemPrice(
            logistics_pb2.EditPriceRequest(item_id=item_id, new_price=new_price),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def edit_item_stock(
        self, item_id: str, delta: int, request_id: str | None = None
    ):
        """Adjust stock by delta (positive = restock, negative = deduction)."""
        return await self._stub().EditItemStock(
            logistics_pb2.EditStockRequest(item_id=item_id, delta=delta),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def remove_item(self, item_id: str, request_id: str | None = None):
        return await self._stub().RemoveItem(
            logistics_pb2.ItemId(id=item_id),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    # ── Quotes & Orders ───────────────────────────────────────────────────────

    async def calculate_quote(
        self,
        lat: float,
        lon: float,
        cart_dict: dict,
        request_id: str | None = None,
    ):
        cart_items = [
            logistics_pb2.CartItem(item_id=str(k), quantity=int(v.get("qty", 0)))
            for k, v in cart_dict.items()
            if v.get("qty", 0) > 0
        ]
        return await self._stub().CalculateQuote(
            logistics_pb2.QuoteRequest(
                user_lat=lat, user_lon=lon, cart_items=cart_items
            ),
            metadata=_meta(request_id),
            timeout=12.0,
        )

    async def place_order(
        self,
        user_id: int,
        username: str,
        quote_data: bytes,
        expected_total: float,
        receipt_text: str,
        request_id: str | None = None,
    ):
        return await self._stub().PlaceOrder(
            logistics_pb2.OrderRequest(
                user_id=int(user_id),
                username=str(username),
                quote_data=quote_data,
                expected_total=float(expected_total),
                receipt_text=str(receipt_text),
            ),
            metadata=_meta(request_id),
            timeout=15.0,
        )

    async def get_orders(self, user_id: int, request_id: str | None = None):
        return await self._stub().GetActiveOrders(
            logistics_pb2.UserId(user_id=int(user_id)),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def get_all_orders(
        self,
        status_filter: str = "pending",
        page: int = 0,
        page_size: int = 20,
        request_id: str | None = None,
    ):
        """Admin: paginated order list filtered by status."""
        return await self._stub().GetAllOrders(
            logistics_pb2.GetAllOrdersRequest(
                status_filter=status_filter,
                page=page,
                page_size=page_size,
            ),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def update_order_status(
        self, order_id: str, new_status: str, request_id: str | None = None
    ):
        return await self._stub().UpdateOrderStatus(
            logistics_pb2.UpdateStatusRequest(
                order_id=order_id, new_status=new_status
            ),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def close(self) -> None:
        for ch in _channels:
            await ch.close()
        logger.info("gRPC channel pool closed")


engine = EngineClient()
