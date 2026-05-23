#!/usr/bin/env python3
"""
assert_thresholds.py
Reads all result JSONs and fails with exit code 1 if any threshold is breached.
"""
import argparse, json, sys
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir",    required=True)
    p.add_argument("--p99-max-ms",     type=float, default=1000)
    p.add_argument("--error-rate-max", type=float, default=0.01)
    p.add_argument("--sqlite-busy-max",type=int,   default=5)
    return p.parse_args()


def load(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def main():
    args   = parse_args()
    d      = Path(args.results_dir)
    failed = []

    # ── gRPC load test ────────────────────────────────────────────────────────
    grpc = load(d / "grpc_results.json")
    if grpc:
        p99 = grpc.get("summary", {}).get("overall_p99_ms", 0)
        if p99 > args.p99_max_ms:
            failed.append(f"gRPC p99 {p99}ms > {args.p99_max_ms}ms threshold")

        total_req = grpc["summary"].get("total_requests", 1)
        total_err = grpc["summary"].get("total_errors", 0)
        err_rate  = total_err / total_req if total_req else 0
        if err_rate > args.error_rate_max:
            failed.append(
                f"gRPC error rate {err_rate*100:.2f}% > {args.error_rate_max*100:.2f}% threshold"
            )

        # Per-phase: recovery phase must look like baseline
        phases = grpc.get("phases", [])
        recovery = next((p for p in phases if p["name"] == "recovery"), None)
        if recovery and recovery["latency_ms"]["p99"] > args.p99_max_ms:
            failed.append(
                f"Recovery p99 {recovery['latency_ms']['p99']}ms — engine did not recover cleanly"
            )

    # ── SQLite stress ─────────────────────────────────────────────────────────
    sqlite = load(d / "sqlite_results.json")
    if sqlite:
        busy = sqlite.get("results", {}).get("total_busy", 0)
        if busy > args.sqlite_busy_max:
            failed.append(
                f"SQLite busy errors {busy} > {args.sqlite_busy_max} threshold"
            )

    # ── Spike test ────────────────────────────────────────────────────────────
    spike = load(d / "spike_results.json")
    if spike:
        err_rate = spike.get("results", {}).get("error_rate", 0)
        if err_rate > args.error_rate_max:
            failed.append(
                f"Spike error rate {err_rate*100:.2f}% > {args.error_rate_max*100:.2f}% threshold"
            )

    # ── Log alerts ────────────────────────────────────────────────────────────
    logs = load(d / "log_metrics.json")
    if logs:
        for alert in logs.get("alerts", []):
            if alert.startswith("CRITICAL"):
                failed.append(f"Engine log: {alert}")

    # ── Report ────────────────────────────────────────────────────────────────
    print("\n=== Threshold Assertions ===")
    if not failed:
        print("✅ All thresholds passed")
        sys.exit(0)
    else:
        print(f"❌ {len(failed)} threshold(s) breached:")
        for msg in failed:
            print(f"   • {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
