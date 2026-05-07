import os
import sys
import grpc
import logging
from api import logistics_pb2, logistics_pb2_grpc

logger = logging.getLogger(__name__)

SOCKET_PATH = 'unix:///app/sockets/logistics.sock'

# Securing the environment mappings natively so it crashes fast instead of blindly failing connection binds later 
INTERNAL_TOKEN = os.getenv('INTERNAL_TOKEN')
if not INTERNAL_TOKEN:
    logger.critical("INTERNAL_TOKEN mapping missing! Setup environment .env vars for Supervisor explicitly.")
    sys.exit(1)


def _get_metadata():
    return (('x-internal-token', INTERNAL_TOKEN),)


class EngineClient:
    def __init__(self):
        # We enforce completely aio gRPC routing logic allowing 0 freeze issues via Async loops directly!
        self.channel = grpc.aio.insecure_channel(SOCKET_PATH)
        self.stub = logistics_pb2_grpc.LogisticsEngineStub(self.channel)
        
        self.DEFAULT_TIMEOUT = 8.0 

    async def close(self):
        """Allows Graceful shutdowns inside bot Supervisor without leaving sockets stranded forever!"""
        if self.channel:
            await self.channel.close()
            logger.info("GRPC channel closed successfully.")

    async def check_health(self):
        req = logistics_pb2.Empty()
        return await self.stub.CheckHealth(req, metadata=_get_metadata(), timeout=3.0)

    async def get_catalog(self):
        req = logistics_pb2.Empty()
        return await self.stub.GetCatalog(req, metadata=_get_metadata(), timeout=self.DEFAULT_TIMEOUT)

    async def add_item(self, item_proto):
        """Missing bound functionality securely linked asynchronously back via Python Bot wizard bindings!"""
        # Mapping item saving natively
        return await self.stub.AddItem(item_proto, metadata=_get_metadata(), timeout=10.0)

    async def calculate_quote(self, lat: float, lon: float, cart_dict: dict):
        cart_items =[
            logistics_pb2.CartItem(item_id=str(k), quantity=int(v.get('qty', 0))) 
            for k, v in cart_dict.items()
        ]
        
        req = logistics_pb2.QuoteRequest(
            user_lat=lat,
            user_lon=lon,
            cart_items=cart_items
        )
        return await self.stub.CalculateQuote(req, metadata=_get_metadata(), timeout=12.0)

    async def place_order(self, user_id: int, username: str, quote_data, expected_total: float, receipt_text: str):
        req = logistics_pb2.OrderRequest(
            user_id=int(user_id),
            username=str(username),
            quote_data=quote_data,
            expected_total=float(expected_total),
            receipt_text=str(receipt_text)
        )
        return await self.stub.PlaceOrder(req, metadata=_get_metadata(), timeout=15.0)


# Initialize our safe module globally
engine = EngineClient()
