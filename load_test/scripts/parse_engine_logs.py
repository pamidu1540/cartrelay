#!/usr/bin/env python3
"""
parse_engine_logs.py
Scans engine logs for Mapbox cache hits/misses, SQLite busy errors,
gRPC errors, and maintenance toggle events.
"""
import argparse, json, re
from pathlib import Path
from collections import defaultdict


PATTERNS = {
    "mapbox_cache_hit":    re.compile(r"mapbox.+cache.+hit",            re.I),
    "mapbox_fetch":        re.compile(r"mapbox.+fetch|fetching.+mapbox", re.I),
    "sqlite_busy":         re.compile(r"SQLITE_BUSY|database is locked|busy timeout", re.I),
    "grpc_error":          re.compile(r"grpc.*error|rpc error",          re.I),
    "resource_exhausted":  re.compile(r"RESOURCE_EXHAUSTED",             re.I),
    "order_placed":        re.compile(r"order.+placed|PlaceOrder.+ok",   re.I),
    "maintenance_toggle":  re.compile(r"maintenance.+toggled|toggle.+maint", re.I),
    "wal_checkpoint":      re.compile(r"wal.+checkpoint|checkpoint",     re.I),
    "panic":               re.compile(r"\bpanic\b",                      re.I),
    "oom":                 re.compile(r"out of memory|OOM|killed",        re.I),
}

GRPC_STATUS_PATTERN = re.compile(
    r"code=(\w+)",
    re.I
)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input",  required=True)
    p.add_argument("--output", required=True)
    return p.parse_args()


def main():
    args = parse_args()
    log_text = Path(args.input).read_text(errors="replace") if Path(args.input).exists() else ""

    counts      = {k: 0 for k in PATTERNS}
    grpc_codes  = defaultdict(int)
    sample_lines = {k: [] for k in PATTERNS}

    for line in log_text.splitlines():
        for name, pat in PATTERNS.items():
            if pat.search(line):
                counts[name] += 1
                if len(sample_lines[name]) < 3:
                    sample_lines[name].append(line.strip()[:200])

        for m in GRPC_STATUS_PATTERN.finditer(line):
            grpc_codes[m.group(1)] += 1

    mapbox_total = counts["mapbox_cache_hit"] + counts["mapbox_fetch"]
    cache_hit_rate = (
        round(counts["mapbox_cache_hit"] / mapbox_total, 4)
        if mapbox_total > 0 else None
    )

    output = {
        "counts":         counts,
        "grpc_codes":     dict(grpc_codes),
        "cache_hit_rate": cache_hit_rate,
        "mapbox_total":   mapbox_total,
        "samples":        {k: v for k, v in sample_lines.items() if v},
        "alerts": [],
    }

    # Flag anything alarming
    if counts["panic"] > 0:
        output["alerts"].append("CRITICAL: panic detected in engine logs")
    if counts["oom"] > 0:
        output["alerts"].append("CRITICAL: OOM / process killed detected")
    if counts["sqlite_busy"] > 5:
        output["alerts"].append(
            f"WARNING: {counts['sqlite_busy']} SQLite busy errors — increase busy_timeout"
        )
    if counts["resource_exhausted"] > 0:
        output["alerts"].append(
            f"INFO: {counts['resource_exhausted']} RESOURCE_EXHAUSTED — LimitListener hit"
        )
    if cache_hit_rate is not None and cache_hit_rate < 0.5:
        output["alerts"].append(
            f"WARNING: Mapbox cache hit rate low: {cache_hit_rate*100:.1f}%"
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2))

    print(f"Log metrics written to {args.output}")
    print(f"  SQLite busy: {counts['sqlite_busy']} | "
          f"gRPC errors: {counts['grpc_error']} | "
          f"Mapbox cache rate: {cache_hit_rate}")
    for alert in output["alerts"]:
        print(f"  !! {alert}")


if __name__ == "__main__":
    main()
