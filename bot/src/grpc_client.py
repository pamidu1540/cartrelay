import asyncio
import concurrent.futures
import hashlib
import hmac
import logging
import os
import sys
import time
import uuid

# ── Use SYNCHRONOUS gRPC, not grpc.aio ────────────────────────────────────────
# grpc.aio binds its completion queue to the event loop active at init time.
# In Python 3.12 + grpcio 1.80, that loop is always different from the one
# asyncio.run() creates inside run_polling() → "Future attached to a different
# loop" on every RPC call. Sync gRPC channels carry zero event-loop state and
# are thread-safe. asyncio.to_thread() offloads each blocking call to the
# default ThreadPoolExecutor so the bot's event loop is never blocked.
import grpc

from api import logistics_pb2, logistics_pb2_grpc

logger = logging.getLogger(__name__)

SOCKET_PATH    = "unix:///app/sockets/logistics.sock"
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN")
if not INTERNAL_TOKEN:
    logger.critical("INTERNAL_TOKEN not set — engine auth will fail on every call.")
    sys.exit(1)

_CHANNEL_OPTIONS = [
    ("grpc.keepalive_time_ms",              10_000),
    ("grpc.keepalive_timeout_ms",            3_000),
    ("grpc.keepalive_permit_without_calls",      1),
    ("grpc.http2.max_pings_without_data",        0),
    ("grpc.http2.min_time_between_pings_ms",  5_000),
]


def _meta(request_id: str | None = None) -> list[tuple[str, str]]:
    ts     = str(int(time.time()))
    req_id = request_id or str(uuid.uuid4())
    payload   = f"{ts}:{req_id}".encode()
    signature = hmac.new(INTERNAL_TOKEN.encode(), payload, hashlib.sha256).hexdigest()
    return [
        ("x-timestamp",      ts),
        ("x-request-id",     req_id),
        ("x-hmac-signature", signature),
    ]


class EngineClient:
    """
    Async wrapper around the LogisticsEngine gRPC stub.

    Uses a synchronous gRPC channel so there is no event-loop binding at all.
    Every public method offloads the blocking RPC to a ThreadPoolExecutor,
    keeping the bot's event loop free and eliminating the
    'Future attached to a different loop' error permanently.
    """

    DEFAULT_TIMEOUT = 8.0

    def __init__(self) -> None:
        self._channel:  grpc.Channel | None = None
        self._stub_obj: logistics_pb2_grpc.LogisticsEngineStub | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

    async def _call(self, func, *args, **kwargs):
        # run_in_executor does not support **kwargs directly.
        # Wrapping in a lambda captures args/kwargs by closure so the stub
        # callable receives them correctly inside the worker thread.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: func(*args, **kwargs)
        )

    async def init_pool(self) -> None:
        if self._channel is not None:
            return
        self._channel  = grpc.insecure_channel(SOCKET_PATH, options=_CHANNEL_OPTIONS)
        self._stub_obj = logistics_pb2_grpc.LogisticsEngineStub(self._channel)
        logger.info("gRPC sync channel initialised → %s", SOCKET_PATH)

    def _stub(self) -> logistics_pb2_grpc.LogisticsEngineStub:
        if self._stub_obj is None:
            raise RuntimeError("gRPC channel not initialised — call init_pool() first.")
        return self._stub_obj

    async def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel  = None
            self._stub_obj = None
            logger.info("gRPC channel closed.")
        self._executor.shutdown(wait=False)
        logger.info("gRPC thread pool shut down.")

    # ── Health & Maintenance ──────────────────────────────────────────────────

    async def check_health(self, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.CheckHealth, logistics_pb2.Empty(), metadata=meta, timeout=3.0)

    async def toggle_maintenance(self, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.ToggleMaintenance, logistics_pb2.Empty(), metadata=meta, timeout=5.0)

    # ── Catalog ───────────────────────────────────────────────────────────────

    async def get_catalog(self, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.GetCatalog, logistics_pb2.Empty(), metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    async def add_item(self, item_proto, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.AddItem, item_proto, metadata=meta, timeout=10.0)

    async def edit_item_price(self, item_id: str, new_price: float, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.EditItemPrice,
            logistics_pb2.EditPriceRequest(item_id=item_id, new_price=new_price),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    async def edit_item_stock(self, item_id: str, delta: int, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.EditItemStock,
            logistics_pb2.EditStockRequest(item_id=item_id, delta=delta),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    async def remove_item(self, item_id: str, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.RemoveItem,
            logistics_pb2.ItemId(id=item_id),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    # ── Warehouses ────────────────────────────────────────────────────────────

    async def add_warehouse(self, name: str, lat: float, lon: float,
                            admin_telegram_id: int = 0, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.AddWarehouse,
            logistics_pb2.AddWarehouseRequest(
                name=name, lat=lat, lon=lon, admin_telegram_id=admin_telegram_id),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    async def list_warehouses(self, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.ListWarehouses,
            logistics_pb2.Empty(), metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    # ── Quotes & Orders ───────────────────────────────────────────────────────

    async def calculate_quote(self, lat: float, lon: float, cart_dict: dict,
                              request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        cart_items = [
            logistics_pb2.CartItem(item_id=str(k), quantity=int(v.get("qty", 0)))
            for k, v in cart_dict.items() if v.get("qty", 0) > 0
        ]
        return await self._call(stub.CalculateQuote,
            logistics_pb2.QuoteRequest(user_lat=lat, user_lon=lon, cart_items=cart_items),
            metadata=meta, timeout=12.0)

    async def place_order(self, user_id: int, username: str, quote_data: bytes,
                          expected_total: float, receipt_text: str,
                          request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.PlaceOrder,
            logistics_pb2.OrderRequest(
                user_id=int(user_id), username=str(username),
                quote_data=quote_data, expected_total=float(expected_total),
                receipt_text=str(receipt_text)),
            metadata=meta, timeout=15.0)

    async def get_orders(self, user_id: int, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.GetActiveOrders,
            logistics_pb2.UserId(user_id=int(user_id)),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    async def get_all_orders(self, status_filter: str = "pending", page: int = 0,
                             page_size: int = 20, request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.GetAllOrders,
            logistics_pb2.GetAllOrdersRequest(
                status_filter=status_filter, page=page, page_size=page_size),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)

    async def update_order_status(self, order_id: str, new_status: str,
                                  request_id: str | None = None):
        stub, meta = self._stub(), _meta(request_id)
        return await self._call(stub.UpdateOrderStatus,
            logistics_pb2.UpdateStatusRequest(order_id=order_id, new_status=new_status),
            metadata=meta, timeout=self.DEFAULT_TIMEOUT)


engine = EngineClient()
