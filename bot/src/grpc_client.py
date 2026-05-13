import hashlib
import hmac
import logging
import os
import sys
import time
import uuid
from itertools import cycle

import grpc

from api import logistics_pb2, logistics_pb2_grpc

logger = logging.getLogger(__name__)

# Constants and Configuration
SOCKET_PATH = "unix:///app/sockets/logistics.sock"
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN")
if not INTERNAL_TOKEN:
    logger.critical("INTERNAL_TOKEN not set — engine auth will fail on every call.")
    sys.exit(1)

_POOL_SIZE = 8

_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms",              10_000),
    ("grpc.keepalive_timeout_ms",            3_000),
    ("grpc.keepalive_permit_without_calls",      1),
    ("grpc.http2.max_pings_without_data",        0),
    ("grpc.http2.min_time_between_pings_ms",  5_000),
]

# Global pool variables - populated during init_pool()
_channels = []
_pool = None


def _get_channel() -> grpc.aio.Channel:
    if not _pool:
        raise RuntimeError("gRPC pool not initialized. Call init_pool() first.")
    return next(_pool)


def _meta(request_id: str | None = None) -> tuple:
    """Return gRPC call metadata with HMAC auth and correlation id."""
    ts = str(int(time.time()))
    req_id = request_id or str(uuid.uuid4())

    # [FIX SEC-002]: HMAC-SHA256 signature to prevent replay and forgery
    payload = f"{ts}:{req_id}".encode("utf-8")
    signature = hmac.new(INTERNAL_TOKEN.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    return (
        ("x-timestamp", ts),
        ("x-request-id", req_id),
        ("x-hmac-signature", signature),
    )


class EngineClient:
    """Thin async wrapper around the LogisticsEngine gRPC stub."""

    DEFAULT_TIMEOUT = 8.0

    async def init_pool(self) -> None:
        """
        [FIX]: Initializes the gRPC channel pool inside the correct active
        event loop to prevent the 'Future attached to different loop' error.
        """
        global _channels, _pool
        if _channels:
            return  # Prevent double initialization

        _channels = [
            grpc.aio.insecure_channel(SOCKET_PATH, options=_CHANNEL_OPTIONS)
            for _ in range(_POOL_SIZE)
        ]
        _pool = cycle(_channels)
        logger.info("gRPC channel pool initialized successfully with %d channels.", _POOL_SIZE)

    def _stub(self) -> logistics_pb2_grpc.LogisticsEngineStub:
        return logistics_pb2_grpc.LogisticsEngineStub(_get_channel())

    # ── Health & Maintenance ──────────────────────────────────────────────────

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

    # ── Catalog Management ────────────────────────────────────────────────────

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

    # ── Warehouse Management (Phase 1) ────────────────────────────────────────

    async def add_warehouse(self, name: str, lat: float, lon: float, request_id: str | None = None):
        return await self._stub().AddWarehouse(
            logistics_pb2.AddWarehouseRequest(name=name, lat=lat, lon=lon),
            metadata=_meta(request_id),
            timeout=self.DEFAULT_TIMEOUT,
        )

    async def list_warehouses(self, request_id: str | None = None):
        return await self._stub().ListWarehouses(
            logistics_pb2.Empty(),
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
        """Gracefully close all channels in the pool."""
        for ch in _channels:
            await ch.close()
        _channels.clear()
        logger.info("gRPC channel pool closed.")


engine = EngineClient()
