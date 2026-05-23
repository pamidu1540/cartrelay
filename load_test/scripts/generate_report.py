#!/usr/bin/env python3
"""
generate_report.py
Produces a self-contained HTML report with inline Chart.js visualisations.
"""
import argparse, json, html
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", required=True)
    p.add_argument("--scenario",    default="full")
    p.add_argument("--max-users",   default="256")
    p.add_argument("--duration",    default="60")
    p.add_argument("--commit",      default="unknown")
    p.add_argument("--run-id",      default="unknown")
    p.add_argument("--output",      required=True)
    return p.parse_args()


def load(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def safe(v, fallback=0):
    return v if v is not None else fallback


def build_report(args, grpc, sqlite, spike, logs):
    # ── Extract key numbers ───────────────────────────────────────────────────
    grpc_phases  = grpc.get("phases", []) if grpc else []
    phase_labels = json.dumps([p["name"] for p in grpc_phases])
    phase_p50    = json.dumps([safe(p.get("latency_ms", {}).get("p50")) for p in grpc_phases])
    phase_p99    = json.dumps([safe(p.get("latency_ms", {}).get("p99")) for p in grpc_phases])
    phase_rps    = json.dumps([safe(p.get("rps")) for p in grpc_phases])
    phase_errors = json.dumps([safe(p.get("error_count")) for p in grpc_phases])

    spike_buckets = spike.get("timeline_buckets", []) if spike else []
    spike_t       = json.dumps([b["t_ms"] for b in spike_buckets])
    spike_count   = json.dumps([b["count"] for b in spike_buckets])
    spike_p99     = json.dumps([b["p99_ms"] for b in spike_buckets])

    grpc_summary  = grpc.get("summary", {})  if grpc   else {}
    sqlite_r      = sqlite.get("results", {}) if sqlite else {}
    sqlite_lat    = sqlite.get("latency_ms",{}) if sqlite else {}
    spike_r       = spike.get("results", {})  if spike  else {}
    spike_lat     = spike.get("latency_ms",{}) if spike  else {}
    log_counts    = logs.get("counts", {})    if logs   else {}
    log_alerts    = logs.get("alerts", [])    if logs   else []
    cache_rate    = logs.get("cache_hit_rate") if logs  else None

    def status_color(v, warn, crit):
        if v > crit:  return "#ef4444"
        if v > warn:  return "#f59e0b"
        return "#22c55e"

    p99_val  = safe(grpc_summary.get("overall_p99_ms"))
    err_rate = round(safe(grpc_summary.get("total_errors")) /
                     max(safe(grpc_summary.get("total_requests"), 1), 1) * 100, 2)

    alert_html = ""
    if log_alerts:
        items = "".join(f"<li>{html.escape(a)}</li>" for a in log_alerts)
        alert_html = f'<div class="alert-box"><ul>{items}</ul></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Load Test Report — {html.escape(args.scenario)}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

  :root {{
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --green:   #22c55e;
    --yellow:  #f59e0b;
    --red:     #ef4444;
    --blue:    #58a6ff;
    --accent:  #1f6feb;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }}

  header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  header h1 {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 18px;
    font-weight: 600;
    color: var(--blue);
    letter-spacing: -0.5px;
  }}
  .meta {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    text-align: right;
    line-height: 1.8;
  }}

  main {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 32px;
    display: grid;
    gap: 24px;
  }}

  .section-title {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}

  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px 24px;
  }}

  .kpi-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
  }}
  .kpi {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }}
  .kpi-label {{
    font-size: 11px;
    color: var(--muted);
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: 1px;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .kpi-value {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 24px;
    font-weight: 600;
    line-height: 1;
  }}
  .kpi-sub {{
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
  }}

  .chart-wrap {{
    position: relative;
    height: 220px;
    margin-top: 16px;
  }}
  .chart-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-top: 16px;
  }}

  table {{
    width: 100%;
    border-collapse: collapse;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    margin-top: 14px;
  }}
  th {{
    text-align: left;
    color: var(--muted);
    padding: 6px 12px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    letter-spacing: 0.5px;
  }}
  td {{
    padding: 7px 12px;
    border-bottom: 1px solid rgba(48,54,61,0.5);
  }}
  tr:hover td {{ background: rgba(31,111,235,0.05); }}

  .pill {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 10px;
    font-weight: 600;
    font-family: 'IBM Plex Mono', monospace;
  }}
  .pill-green  {{ background: rgba(34,197,94,0.15);  color: var(--green);  }}
  .pill-yellow {{ background: rgba(245,158,11,0.15); color: var(--yellow); }}
  .pill-red    {{ background: rgba(239,68,68,0.15);  color: var(--red);    }}

  .alert-box {{
    background: rgba(239,68,68,0.08);
    border: 1px solid rgba(239,68,68,0.3);
    border-radius: 6px;
    padding: 12px 16px;
    margin-top: 12px;
  }}
  .alert-box ul {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .alert-box li {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: var(--red);
  }}
  .alert-box li::before {{ content: "⚠ "; }}

  .two-col {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
  }}
  @media (max-width: 768px) {{
    .two-col, .chart-grid {{ grid-template-columns: 1fr; }}
    main {{ padding: 16px; }}
  }}
</style>
</head>
<body>

<header>
  <h1>logistics-engine-core / load test</h1>
  <div class="meta">
    <div>scenario: {html.escape(args.scenario)}</div>
    <div>max users: {html.escape(args.max_users)} · duration: {html.escape(args.duration)}s</div>
    <div>commit: {html.escape(args.commit[:8])}</div>
    <div>run: #{html.escape(args.run_id)}</div>
  </div>
</header>

<main>

  <!-- Alerts -->
  {alert_html}

  <!-- KPIs -->
  <div class="card">
    <div class="section-title">Summary KPIs</div>
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label">Peak RPS</div>
        <div class="kpi-value" style="color:var(--blue)">{safe(grpc_summary.get("peak_rps"))}</div>
        <div class="kpi-sub">requests / second</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">gRPC p99</div>
        <div class="kpi-value" style="color:{status_color(p99_val,500,1000)}">{p99_val}<span style="font-size:14px">ms</span></div>
        <div class="kpi-sub">threshold: 1000ms</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Error Rate</div>
        <div class="kpi-value" style="color:{status_color(err_rate,0.5,1)}">{err_rate}<span style="font-size:14px">%</span></div>
        <div class="kpi-sub">threshold: 1%</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">SQLite Busy</div>
        <div class="kpi-value" style="color:{status_color(safe(sqlite_r.get('total_busy')),1,5)}">{safe(sqlite_r.get("total_busy"))}</div>
        <div class="kpi-sub">threshold: 5</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Cache Hit Rate</div>
        <div class="kpi-value" style="color:var(--green)">{f"{cache_rate*100:.1f}" if cache_rate is not None else "—"}<span style="font-size:14px">%</span></div>
        <div class="kpi-sub">Mapbox distance cache</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Spike Errors</div>
        <div class="kpi-value" style="color:{status_color(safe(spike_r.get('error_rate',0))*100,0.5,1)}">{round(safe(spike_r.get("error_rate",0))*100,2)}<span style="font-size:14px">%</span></div>
        <div class="kpi-sub">0→{args.max_users} users in {spike.get("config",{{}}).get("ramp_secs","?") if spike else "?"}s</div>
      </div>
    </div>
  </div>

  <!-- gRPC charts -->
  <div class="card">
    <div class="section-title">gRPC Engine — Phase Breakdown</div>
    <div class="chart-grid">
      <div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:4px">Latency by phase (ms)</div>
        <div class="chart-wrap"><canvas id="latencyChart"></canvas></div>
      </div>
      <div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:4px">Throughput by phase (RPS)</div>
        <div class="chart-wrap"><canvas id="rpsChart"></canvas></div>
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th>Phase</th><th>Concurrency</th><th>Total</th>
          <th>p50</th><th>p95</th><th>p99</th><th>Errors</th>
        </tr>
      </thead>
      <tbody>
        {"".join(
            f"<tr>"
            f"<td>{html.escape(p['name'])}</td>"
            f"<td>{p.get('concurrency','-')}</td>"
            f"<td>{p.get('total_requests','-')}</td>"
            f"<td>{safe(p.get('latency_ms',{{}}).get('p50'))}ms</td>"
            f"<td>{safe(p.get('latency_ms',{{}}).get('p95'))}ms</td>"
            f"<td>{safe(p.get('latency_ms',{{}}).get('p99'))}ms</td>"
            f"<td><span class='pill {'pill-red' if p.get('error_count',0)>0 else 'pill-green'}'>"
            f"{p.get('error_count',0)}/{p.get('total_requests',0)}</span></td>"
            f"</tr>"
            for p in grpc_phases
        )}
      </tbody>
    </table>
  </div>

  <!-- Spike chart -->
  <div class="card">
    <div class="section-title">Spike Test — Timeline</div>
    <div class="chart-wrap"><canvas id="spikeChart"></canvas></div>
  </div>

  <!-- SQLite + Log side by side -->
  <div class="two-col">
    <div class="card">
      <div class="section-title">SQLite Write Contention</div>
      <table>
        <tr><td>Writers</td><td><b>{safe(sqlite.get("config",{{}}).get("writers")) if sqlite else "—"}</b></td></tr>
        <tr><td>Writes/sec</td><td><b>{safe(sqlite_r.get("writes_per_sec"))}</b></td></tr>
        <tr><td>Busy errors</td><td style="color:{status_color(safe(sqlite_r.get('total_busy')),1,5)}"><b>{safe(sqlite_r.get("total_busy"))}</b></td></tr>
        <tr><td>Busy rate</td><td><b>{round(safe(sqlite_r.get("busy_rate"))*100,2)}%</b></td></tr>
        <tr><td>p50 write lat</td><td><b>{safe(sqlite_lat.get("p50"))}ms</b></td></tr>
        <tr><td>p99 write lat</td><td><b>{safe(sqlite_lat.get("p99"))}ms</b></td></tr>
      </table>
    </div>
    <div class="card">
      <div class="section-title">Engine Log Signals</div>
      <table>
        <tr><td>Orders placed</td><td><b>{safe(log_counts.get("order_placed"))}</b></td></tr>
        <tr><td>Mapbox cache hits</td><td><b>{safe(log_counts.get("mapbox_cache_hit"))}</b></td></tr>
        <tr><td>Mapbox API fetches</td><td><b>{safe(log_counts.get("mapbox_fetch"))}</b></td></tr>
        <tr><td>SQLite busy</td><td style="color:{status_color(safe(log_counts.get('sqlite_busy')),1,5)}"><b>{safe(log_counts.get("sqlite_busy"))}</b></td></tr>
        <tr><td>gRPC errors</td><td style="color:{status_color(safe(log_counts.get('grpc_error')),5,20)}"><b>{safe(log_counts.get("grpc_error"))}</b></td></tr>
        <tr><td>RESOURCE_EXHAUSTED</td><td><b>{safe(log_counts.get("resource_exhausted"))}</b></td></tr>
        <tr><td>Panics</td><td style="color:var({'--red' if safe(log_counts.get('panic'))>0 else '--green'})"><b>{safe(log_counts.get("panic"))}</b></td></tr>
      </table>
    </div>
  </div>

</main>

<script>
const CHART_DEFAULTS = {{
  responsive: true,
  maintainAspectRatio: false,
  plugins: {{ legend: {{ labels: {{ color: '#8b949e', font: {{ family: 'IBM Plex Mono', size: 11 }} }} }} }},
  scales: {{
    x: {{ ticks: {{ color: '#8b949e', font: {{ family: 'IBM Plex Mono', size: 10 }} }}, grid: {{ color: 'rgba(48,54,61,0.5)' }} }},
    y: {{ ticks: {{ color: '#8b949e', font: {{ family: 'IBM Plex Mono', size: 10 }} }}, grid: {{ color: 'rgba(48,54,61,0.5)' }} }},
  }}
}};

// Latency chart
new Chart(document.getElementById('latencyChart'), {{
  type: 'bar',
  data: {{
    labels: {phase_labels},
    datasets: [
      {{ label: 'p50', data: {phase_p50}, backgroundColor: 'rgba(88,166,255,0.6)', borderRadius: 3 }},
      {{ label: 'p99', data: {phase_p99}, backgroundColor: 'rgba(239,68,68,0.6)',  borderRadius: 3 }},
    ]
  }},
  options: {{ ...CHART_DEFAULTS, plugins: {{ ...CHART_DEFAULTS.plugins,
    tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ' + ctx.raw + 'ms' }} }}
  }} }}
}});

// RPS chart
new Chart(document.getElementById('rpsChart'), {{
  type: 'line',
  data: {{
    labels: {phase_labels},
    datasets: [
      {{ label: 'RPS', data: {phase_rps}, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)', tension: 0.3, fill: true, pointRadius: 4 }},
    ]
  }},
  options: CHART_DEFAULTS,
}});

// Spike timeline
new Chart(document.getElementById('spikeChart'), {{
  type: 'line',
  data: {{
    labels: {spike_t},
    datasets: [
      {{ label: 'Requests', data: {spike_count}, borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.1)', tension: 0.3, fill: true, yAxisID: 'y' }},
      {{ label: 'p99 (ms)', data: {spike_p99}, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.3, yAxisID: 'y1' }},
    ]
  }},
  options: {{
    ...CHART_DEFAULTS,
    scales: {{
      x: {{ ...CHART_DEFAULTS.scales.x, title: {{ display: true, text: 'Time (ms)', color: '#8b949e' }} }},
      y:  {{ ...CHART_DEFAULTS.scales.y, title: {{ display: true, text: 'Requests', color: '#8b949e' }} }},
      y1: {{ ...CHART_DEFAULTS.scales.y, position: 'right', title: {{ display: true, text: 'p99 ms', color: '#f59e0b' }}, grid: {{ drawOnChartArea: false }} }},
    }}
  }}
}});
</script>
</body>
</html>"""


def main():
    args  = parse_args()
    d     = Path(args.results_dir)
    grpc  = load(d / "grpc_results.json")
    sqlite= load(d / "sqlite_results.json")
    spike = load(d / "spike_results.json")
    logs  = load(d / "log_metrics.json")

    report = build_report(args, grpc, sqlite, spike, logs)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(report)
    print(f"HTML report written to {args.output}")


if __name__ == "__main__":
    main()
