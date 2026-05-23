#!/usr/bin/env python3
"""
run_grpc_load.py
Runs phased gRPC load test against the engine and writes JSON results.
Phases: baseline(10) → ramp(50→256) → sustained → recovery
"""
import argparse, asyncio, json, time, sys, statistics
from pathlib import Path

try:
    import grpc
    from concurrent.futures import ThreadPoolExecutor
    from api import logistics_pb2, logistics_pb2_grpc
except ImportError as e:
    print(f"ImportError: {e}", file=sys.stderr)
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--token",     required=True)
    p.add_argument("--host",      default="localhost:50051")
    p.add_argument("--max-users", type=int, default=256)
    p.add_argument("--duration",  type=int, default=60, help="Sustained phase seconds")
    p.add_argument("--output",    required=True)
    return p.parse_args()


def make_metadata(token: str):
    return [("authorization", f"Bearer {token}")]


def call_engine(stub, token: str, user_id: int) -> dict:
    """Simulate one user's full order flow. Returns timing + error info."""
    meta = (("authorization", f"Bearer {token}"),)
    result = {"user_id": user_id, "steps": {}, "error": None}
    t0 = time.perf_counter()

    try:
        # Step 1: List catalog
        t = time.perf_counter()
        cat_resp = stub.GetCatalog(logistics_pb2.CatalogRequest(page_size=10, page_token=""), metadata=meta)
        result["steps"]["list_catalog_ms"] = round((time.perf_counter() - t) * 1000, 2)

        if not cat_resp.items:
            raise grpc.RpcError("Catalog is empty")
            
        item_id = cat_resp.items[0].id

        # Step 2: Add to cart
        t = time.perf_counter()
        stub.SaveCart(logistics_pb2.SaveCartRequest(
            user_id=str(user_id),
            items=[logistics_pb2.CartItem(item_id=item_id, quantity=1)]
        ), metadata=meta)
        result["steps"]["add_cart_ms"] = round((time.perf_counter() - t) * 1000, 2)

        # Step 3: Generate quote
        t = time.perf_counter()
        stub.CalculateQuote(logistics_pb2.QuoteRequest(
            user_id=str(user_id),
            destination_lat=40.7128,
            destination_lon=-74.0060,
        ), metadata=meta)
        result["steps"]["quote_ms"] = round((time.perf_counter() - t) * 1000, 2)

        result["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    except grpc.RpcError as e:
        result["error"] = {"code": e.code().name, "details": e.details()}
        result["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    return result


def run_phase(name: str, concurrency: int, total: int, token: str, host: str) -> dict:
    """Run a load phase with given concurrency and collect metrics."""
    print(f"  Phase [{name}] concurrency={concurrency} total={total}")
    channel_pool = [grpc.insecure_channel(host) for _ in range(min(8, concurrency))]

    results = []
    start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(call_engine, logistics_pb2_grpc.LogisticsEngineStub(channel_pool[i % len(channel_pool)]), token, i)
            for i in range(total)
        ]
        for f in futures:
            results.append(f.result())

    elapsed = time.perf_counter() - start

    latencies = [r["total_ms"] for r in results if r.get("total_ms") is not None]
    errors    = [r for r in results if r.get("error")]

    phase_stats = {
        "name":            name,
        "concurrency":     concurrency,
        "total_requests":  total,
        "elapsed_s":       round(elapsed, 3),
        "rps":             round(total / elapsed, 1) if elapsed > 0 else 0,
        "error_count":     len(errors),
        "error_rate":      round(len(errors) / len(results), 4) if results else 0,
        "error_breakdown": {},
        "latency_ms": {
            "min":  round(min(latencies), 2) if latencies else 0,
            "p50":  round(statistics.median(latencies), 2) if latencies else 0,
            "p95":  round(sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0, 2),
            "p99":  round(sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0, 2),
            "max":  round(max(latencies), 2) if latencies else 0,
            "mean": round(statistics.mean(latencies), 2) if latencies else 0,
        },
    }

    for e in errors:
        code = e["error"]["code"] if e.get("error") else "UNKNOWN"
        phase_stats["error_breakdown"][code] = phase_stats["error_breakdown"].get(code, 0) + 1

    for ch in channel_pool:
        ch.close()

    return phase_stats


def main():
    args = parse_args()
    print(f"=== gRPC Load Test | host={args.host} | max_users={args.max_users} ===")

    phases = [
        # (name, concurrency, total_requests)
        ("baseline",  10,             100),
        ("ramp_50",   50,             500),
        ("ramp_128",  128,            1000),
        ("peak",      args.max_users, args.max_users * 5),
        ("sustained", args.max_users, args.max_users * max(1, args.duration // 2)),
        ("recovery",  10,             100),
    ]

    all_phases = []
    for name, concurrency, total in phases:
        phase = run_phase(name, concurrency, total, args.token, args.host)
        all_phases.append(phase)
        print(f"    p99={phase['latency_ms']['p99']}ms  "
              f"errors={phase['error_count']}/{phase['total_requests']}  "
              f"rps={phase['rps']}")

    output = {
        "test": "grpc_load",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {"max_users": args.max_users, "duration_s": args.duration, "host": args.host},
        "phases": all_phases,
        "summary": {
            "peak_rps":         max(p["rps"] for p in all_phases),
            "overall_p99_ms":   max(p["latency_ms"]["p99"] for p in all_phases),
            "total_errors":     sum(p["error_count"] for p in all_phases),
            "total_requests":   sum(p["total_requests"] for p in all_phases),
        }
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"\nResults written to {args.output}")
    print(f"Peak RPS: {output['summary']['peak_rps']} | "
          f"Overall p99: {output['summary']['overall_p99_ms']}ms | "
          f"Total errors: {output['summary']['total_errors']}")


if __name__ == "__main__":
    main()
