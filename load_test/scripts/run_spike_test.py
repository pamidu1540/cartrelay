#!/usr/bin/env python3
"""
run_spike_test.py
Ramps from 0 to peak users in ramp_seconds, measures graceful shedding.
"""
import argparse, asyncio, json, time, hmac, hashlib
from pathlib import Path
import grpc
from api import logistics_pb2, logistics_pb2_grpc


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--peak-users",    type=int, default=500)
    p.add_argument("--ramp-seconds",  type=int, default=5)
    p.add_argument("--token",         required=True)
    p.add_argument("--host",          default="localhost:50051")
    p.add_argument("--output",        required=True)
    return p.parse_args()


def make_meta(token: str, method: str) -> tuple:
    minute = int(time.time()) // 60
    msg = f"{method}:{minute}".encode()
    auth_token = hmac.HMAC(token.encode(), msg, hashlib.sha256).hexdigest()
    return (("x-auth-token", auth_token),)


async def fake_user_call(user_id: int, token: str, host: str, timeline: list):
    """Simulate a single user call; records start/end/status in timeline."""
    t_start = time.perf_counter()
    status = "ok"
    try:
        async with grpc.aio.insecure_channel(host) as channel:
            stub = logistics_pb2_grpc.LogisticsEngineStub(channel)
            meta_catalog = make_meta(token, "/logistics.LogisticsEngine/GetCatalog")
            cat_resp = await stub.GetCatalog(logistics_pb2.CatalogRequest(page_size=10, page=0), metadata=meta_catalog)
            if cat_resp.items:
                meta_savecart = make_meta(token, "/logistics.LogisticsEngine/SaveCart")
                await stub.SaveCart(logistics_pb2.SaveCartRequest(
                    user_id=int(user_id),
                    items=[logistics_pb2.CartItem(item_id=cat_resp.items[0].id, quantity=1)]
                ), metadata=meta_savecart)
    except Exception as e:
        status = "error"

    latency = round((time.perf_counter() - t_start) * 1000, 2)

    timeline.append({
        "user_id": user_id,
        "t_start": round(t_start, 3),
        "latency_ms": latency,
        "status": status,
    })


async def run_spike(peak: int, ramp_secs: int, token: str, host: str):
    """Schedule peak users spread across ramp_secs seconds."""
    timeline = []
    tasks = []
    interval = ramp_secs / peak  # seconds between each new user spawned

    t0 = time.perf_counter()
    for i in range(peak):
        await asyncio.sleep(interval)
        tasks.append(asyncio.create_task(fake_user_call(i, token, host, timeline)))

    await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - t0

    return timeline, elapsed


def bucket_timeline(timeline: list, bucket_size_ms: float = 500) -> list:
    """Group results into time buckets for charting."""
    if not timeline:
        return []

    t_min = min(e["t_start"] for e in timeline)
    buckets = {}
    for e in timeline:
        b = int(((e["t_start"] - t_min) * 1000) / bucket_size_ms) * bucket_size_ms
        if b not in buckets:
            buckets[b] = {"t_ms": b, "count": 0, "errors": 0, "latencies": []}
        buckets[b]["count"] += 1
        buckets[b]["latencies"].append(e["latency_ms"])
        if e["status"] != "ok":
            buckets[b]["errors"] += 1

    result = []
    for b in sorted(buckets):
        d = buckets[b]
        lats = sorted(d["latencies"])
        n = len(lats)
        result.append({
            "t_ms":       d["t_ms"],
            "count":      d["count"],
            "errors":     d["errors"],
            "p99_ms":     round(lats[int(n * 0.99)], 2) if n else 0,
            "p50_ms":     round(lats[n // 2], 2) if n else 0,
        })
    return result


def main():
    args = parse_args()
    print(f"=== Spike Test | peak={args.peak_users} ramp={args.ramp_seconds}s ===")

    timeline, elapsed = asyncio.run(
        run_spike(args.peak_users, args.ramp_seconds, args.token, args.host)
    )

    errors  = [e for e in timeline if e["status"] != "ok"]
    success = [e for e in timeline if e["status"] == "ok"]
    lats    = sorted(e["latency_ms"] for e in success)
    n       = len(lats)

    output = {
        "test":      "spike",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "peak_users":   args.peak_users,
            "ramp_secs":    args.ramp_seconds,
        },
        "results": {
            "elapsed_s":   round(elapsed, 3),
            "total":       len(timeline),
            "success":     len(success),
            "errors":      len(errors),
            "error_rate":  round(len(errors) / len(timeline), 4) if timeline else 0,
        },
        "latency_ms": {
            "p50":  round(lats[n // 2], 2) if n else 0,
            "p95":  round(lats[int(n * 0.95)], 2) if n else 0,
            "p99":  round(lats[int(n * 0.99)], 2) if n else 0,
            "max":  round(lats[-1], 2) if n else 0,
        },
        "timeline_buckets": bucket_timeline(timeline),
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"  Errors: {len(errors)}/{len(timeline)} | "
          f"p99: {output['latency_ms']['p99']}ms | "
          f"elapsed: {round(elapsed,2)}s")
    print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
