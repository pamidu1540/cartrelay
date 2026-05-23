#!/usr/bin/env python3
"""
run_sqlite_stress.py
Fires concurrent writers at the SQLite WAL DB and measures lock contention.
"""
import argparse, json, sqlite3, threading, time
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--db",          default="deploy/data/engine.db")
    p.add_argument("--writers",     type=int, default=100)
    p.add_argument("--writes-each", type=int, default=50)
    p.add_argument("--output",      required=True)
    return p.parse_args()


def writer_thread(tid: int, db_path: str, writes: int, results: list, lock: threading.Lock):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    success, busy_errors, other_errors = 0, 0, 0
    latencies = []

    for i in range(writes):
        t0 = time.perf_counter()
        try:
            conn.execute(
                "INSERT INTO audit_log (action, actor, detail, ts) "
                "VALUES (?, ?, ?, strftime('%s','now'))",
                ("load_test", f"thread_{tid}", f"write_{i}")
            )
            conn.commit()
            success += 1
            latencies.append(round((time.perf_counter() - t0) * 1000, 3))
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                busy_errors += 1
            else:
                other_errors += 1
        except Exception:
            other_errors += 1

    conn.close()
    with lock:
        results.append({
            "thread_id":    tid,
            "success":      success,
            "busy_errors":  busy_errors,
            "other_errors": other_errors,
            "latencies_ms": latencies,
        })


def main():
    args = parse_args()
    print(f"=== SQLite Stress | writers={args.writers} writes_each={args.writes_each} ===")

    if not Path(args.db).exists():
        print(f"DB not found at {args.db} — engine may not be running. "
              "Creating temp in-memory proxy for CI.")
        args.db = "/tmp/load_test_ci.db"
        conn = sqlite3.connect(args.db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log "
            "(id INTEGER PRIMARY KEY, action TEXT, actor TEXT, detail TEXT, ts INTEGER)"
        )
        conn.commit()
        conn.close()

    results = []
    lock    = threading.Lock()
    threads = [
        threading.Thread(
            target=writer_thread,
            args=(i, args.db, args.writes_each, results, lock)
        )
        for i in range(args.writers)
    ]

    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    total_success = sum(r["success"]      for r in results)
    total_busy    = sum(r["busy_errors"]  for r in results)
    total_other   = sum(r["other_errors"] for r in results)
    all_latencies = [l for r in results for l in r["latencies_ms"]]

    sorted_lat = sorted(all_latencies)
    n = len(sorted_lat)

    output = {
        "test":      "sqlite_stress",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "writers":      args.writers,
            "writes_each":  args.writes_each,
            "total_writes": args.writers * args.writes_each,
            "db":           args.db,
        },
        "results": {
            "elapsed_s":       round(elapsed, 3),
            "total_success":   total_success,
            "total_busy":      total_busy,
            "total_other":     total_other,
            "busy_rate":       round(total_busy / (total_success + total_busy + total_other + 1e-9), 4),
            "writes_per_sec":  round(total_success / elapsed, 1),
        },
        "latency_ms": {
            "min":  round(sorted_lat[0],              2) if n else 0,
            "p50":  round(sorted_lat[n // 2],         2) if n else 0,
            "p95":  round(sorted_lat[int(n * 0.95)],  2) if n else 0,
            "p99":  round(sorted_lat[int(n * 0.99)],  2) if n else 0,
            "max":  round(sorted_lat[-1],             2) if n else 0,
        },
        "per_thread": results,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, indent=2))

    print(f"  Success: {total_success} | Busy: {total_busy} | Other: {total_other}")
    print(f"  Writes/s: {output['results']['writes_per_sec']} | "
          f"p99: {output['latency_ms']['p99']}ms | "
          f"Busy rate: {output['results']['busy_rate']*100:.2f}%")
    print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
