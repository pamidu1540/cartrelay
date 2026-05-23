#!/usr/bin/env python3
"""
write_summary.py
Writes a GitHub Actions job summary in Markdown to stdout.
"""
import argparse, json
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", required=True)
    return p.parse_args()


def load(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def badge(value, warn, crit, unit="", invert=False):
    """Return a coloured emoji for a metric."""
    bad = value > crit if not invert else value < crit
    med = value > warn if not invert else value < warn
    if bad:   return f"🔴 **{value}{unit}**"
    if med:   return f"🟡 **{value}{unit}**"
    return f"🟢 **{value}{unit}**"


def main():
    args = parse_args()
    d = Path(args.results_dir)

    grpc   = load(d / "grpc_results.json")
    sqlite = load(d / "sqlite_results.json")
    spike  = load(d / "spike_results.json")
    logs   = load(d / "log_metrics.json")

    lines = ["# 🚦 Load Test Report\n"]

    # ── gRPC summary ──────────────────────────────────────────────────────────
    if grpc:
        s = grpc.get("summary", {})
        lines += [
            "## gRPC Engine Load\n",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Peak RPS       | {badge(s.get('peak_rps',0), 100, 50, ' req/s', invert=True)} |",
            f"| Overall p99    | {badge(s.get('overall_p99_ms',0), 500, 1000, 'ms')} |",
            f"| Total requests | **{s.get('total_requests',0):,}** |",
            f"| Total errors   | {badge(s.get('total_errors',0), 1, 10)} |",
            "",
            "### Phase breakdown\n",
            "| Phase | Concurrency | RPS | p50 | p99 | Errors |",
            "|-------|-------------|-----|-----|-----|--------|",
        ]
        for p in grpc.get("phases", []):
            lat = p.get("latency_ms", {})
            lines.append(
                f"| {p['name']} | {p['concurrency']} "
                f"| {p['rps']} "
                f"| {lat.get('p50','-')}ms "
                f"| {lat.get('p99','-')}ms "
                f"| {p['error_count']}/{p['total_requests']} |"
            )
        lines.append("")

    # ── SQLite summary ────────────────────────────────────────────────────────
    if sqlite:
        r = sqlite.get("results", {})
        lat = sqlite.get("latency_ms", {})
        lines += [
            "## SQLite Write Contention\n",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Writes/sec   | **{r.get('writes_per_sec',0)}** |",
            f"| Busy errors  | {badge(r.get('total_busy',0), 1, 5)} |",
            f"| Busy rate    | {badge(round(r.get('busy_rate',0)*100,2), 0.5, 2, '%')} |",
            f"| p99 latency  | {badge(lat.get('p99',0), 100, 500, 'ms')} |",
            "",
        ]

    # ── Spike summary ─────────────────────────────────────────────────────────
    if spike:
        r   = spike.get("results", {})
        lat = spike.get("latency_ms", {})
        lines += [
            "## Spike Test\n",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Peak users   | **{spike['config']['peak_users']}** in {spike['config']['ramp_secs']}s |",
            f"| Error rate   | {badge(round(r.get('error_rate',0)*100,2), 0.5, 1, '%')} |",
            f"| p99 latency  | {badge(lat.get('p99',0), 500, 1000, 'ms')} |",
            "",
        ]

    # ── Log alerts ────────────────────────────────────────────────────────────
    if logs:
        alerts = logs.get("alerts", [])
        if alerts:
            lines.append("## ⚠️ Engine Log Alerts\n")
            for a in alerts:
                lines.append(f"- {a}")
            lines.append("")

        c = logs.get("counts", {})
        rate = logs.get("cache_hit_rate")
        lines += [
            "## Engine Log Metrics\n",
            "| Signal | Count |",
            "|--------|-------|",
            f"| Mapbox cache hits | **{c.get('mapbox_cache_hit',0)}** |",
            f"| Mapbox API fetches | **{c.get('mapbox_fetch',0)}** |",
            f"| Cache hit rate | **{f'{rate*100:.1f}%' if rate is not None else 'n/a'}** |",
            f"| SQLite busy | {badge(c.get('sqlite_busy',0), 1, 5)} |",
            f"| gRPC errors | {badge(c.get('grpc_error',0), 5, 20)} |",
            f"| Panics | {badge(c.get('panic',0), 0, 0)} |",
            "",
        ]

    print("\n".join(lines))


if __name__ == "__main__":
    main()
