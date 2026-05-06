import os
import grpc
from api import logistics_pb2, logistics_pb2_grpc

SOCKET_PATH = 'unix:///app/sockets/logistics.sock'
INTERNAL_TOKEN = os.getenv('INTERNAL_TOKEN', '')

def _get_metadata():
    return (('x-internal-token', INTERNAL_TOKEN),)

class EngineClient:
    def __init__(self):
        self.channel = grpc.insecure_channel(SOCKET_PATH)
        self.stub = logistics_pb2_grpc.LogisticsEngineStub(self.channel)

    def check_health(self):
        req = logistics_pb2.Empty()
        return self.stub.CheckHealth(req, metadata=_get_metadata())

    def get_catalog(self):
        req = logistics_pb2.Empty()
        return self.stub.GetCatalog(req, metadata=_get_metadata())

    def calculate_quote(self, lat: float, lon: float, cart_dict: dict):
        cart_items =[
            logistics_pb2.CartItem(item_id=k, quantity=v['qty']) 
            for k, v in cart_dict.items()
        ]
        req = logistics_pb2.QuoteRequest(
            user_lat=lat,
            user_lon=lon,
            cart_items=cart_items
        )
        return self.stub.CalculateQuote(req, metadata=_get_metadata())

    def place_order(self, user_id: int, username: str, quote_data, expected_total: float, receipt_text: str):
        req = logistics_pb2.OrderRequest(
            user_id=user_id,
            username=username,
            quote_data=quote_data,
            expected_total=expected_total,
            receipt_text=receipt_text
        )
        return self.stub.PlaceOrder(req, metadata=_get_metadata())

# Global singleton client
engine = EngineClient()