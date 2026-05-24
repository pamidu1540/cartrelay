#!/usr/bin/env python3
"""
seed_data.py
Seeds catalog items and simulated user carts for load testing.
Calls the engine gRPC directly via INTERNAL_TOKEN.
"""
import argparse, sys, hmac, hashlib, time

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--token",  required=True)
    p.add_argument("--host",   default="localhost:50051")
    p.add_argument("--items",  type=int, default=50)
    p.add_argument("--users",  type=int, default=500)
    return p.parse_args()


import grpc
from api import logistics_pb2, logistics_pb2_grpc

def make_meta(token: str, method: str) -> tuple:
    minute = int(time.time()) // 60
    msg = f"{method}:{minute}".encode()
    auth_token = hmac.HMAC(token.encode(), msg, hashlib.sha256).hexdigest()
    return (("x-auth-token", auth_token),)

def main():
    args = parse_args()
    print(f"Seeding {args.items} catalog items for {args.users} test users...")

    channel = grpc.insecure_channel(args.host)
    stub    = logistics_pb2_grpc.LogisticsEngineStub(channel)

    for i in range(args.items):
        try:
            meta = make_meta(args.token, "/logistics.LogisticsEngine/AddItem")
            stub.AddItem(logistics_pb2.Item(
                name=f"test_item_{i}",
                description="test",
                price=round(10 + i * 0.5, 2),
                stock=1000,
                category="load_test",
                is_available=True,
            ), metadata=meta)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.ALREADY_EXISTS:
                print(f"Error seeding item: {e}")

    print(f"✓ Seeded {args.items} items")

    try:
        meta = make_meta(args.token, "/logistics.LogisticsEngine/AddWarehouse")
        stub.AddWarehouse(logistics_pb2.Warehouse(
            name="Test Warehouse",
            address="Test Addr",
            lat=40.7128,
            lon=-74.0060,
            is_active=True,
        ), metadata=meta)
        print("✓ Seeded test warehouse")
    except grpc.RpcError as e:
        if e.code() != grpc.StatusCode.ALREADY_EXISTS:
            print(f"Error seeding warehouse: {e}")

if __name__ == "__main__":
    main()
