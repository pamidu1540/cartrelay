# Logistics Engine v4

A Telegram-based delivery management system: customers browse a catalog, get a
live delivery quote, and place orders; admins manage catalog and order lifecycle
via an in-chat admin panel.

```
┌─────────────┐   gRPC/Unix socket   ┌──────────────────┐
│ Telegram    │◄────────────────────►│  Engine (Go)      │
│ Bot (Python)│                      │  • gRPC server    │
└─────────────┘                      │  • SQLite + WAL   │
       ▲                             │  • Mapbox routing │
       │ HTTPS                       └──────────────────┘
  Telegram API                              ▲
                                            │ Docker SDK / gRPC
                                    ┌───────────────┐
                                    │ Supervisor    │
                                    │ (Go CLI + TUI)│
                                    └───────────────┘
```

---

## Quick start

```bash
cp deploy/.env.example deploy/.env
# Edit deploy/.env — set TELEGRAM_BOT_TOKEN, INTERNAL_TOKEN, MAPBOX_TOKEN

# Local dev (no license check):
echo "DEV_BYPASS_SECRET=$(openssl rand -hex 16)" >> deploy/.env
echo "SKIP_LICENSE_CHECK=$(grep DEV_BYPASS_SECRET deploy/.env | cut -d= -f2)" >> deploy/.env

# Build and start
cd deploy
docker compose up -d --build
```

---

## Supervisor CLI

| Command | Description |
|---|---|
| `logistics start` | Start all services |
| `logistics stop` | Stop all services |
| `logistics restart` | Restart containers |
| `logistics status` | One-shot health snapshot |
| `logistics logs` | Follow all logs |
| `logistics logs -s engine` | Follow engine logs only |
| `logistics logs -s bot` | Follow bot logs only |
| `logistics dashboard` | Interactive TUI |
| `logistics update` | Apply OTA update |
| `logistics update --dry-run` | Check what update is available |
| `logistics rollback` | Re-apply previous tarball |
| `logistics maintenance` | Toggle engine maintenance mode |

---

## Key fixes in v4 (vs v3)

| # | Fix |
|---|---|
| 1 | `INTERNAL_TOKEN=""` no longer authenticates — engine refuses to start if unset |
| 2 | `quote_data` is now mandatory; callers cannot skip the price-integrity check |
| 3 | Stock is decremented atomically in the same transaction that inserts the order |
| 4 | Carts moved from the `config` table to a dedicated `carts` table with TTL |
| 5 | `UpdateOrderStatus` returns `notify_user_id` as a typed proto field, not a string |
| 6 | `FetchMapboxDistance` uses an HTTP client with a 5 s timeout |
| 7 | OTA verifier streams SHA-256 hash instead of loading the whole tarball into RAM |
| 8 | `publicKeyPEM` / `publicKeyHex` placeholder detected at startup with a clear error |
| 9 | `nearestDepot` uses `strconv.ParseFloat` — float parse errors no longer silently zero |
| 10 | Item and order IDs use UUIDv4 — no collision risk under concurrent load |
| 11 | `Version` injected at build time via `-ldflags`; not hardcoded to `"3.0.0"` |
| 12 | `GetConfigValue` returns `(string, error)` — DB failures no longer silently return `""` |
| 13 | `GRPCToggleMaintenance` uses the generated `pb.StatusResponse` type |

## Optimisations in v4

| Area | Change |
|---|---|
| SQLite | `MaxOpenConns=1`, 64 MB page cache, 256 MB mmap, 5 s busy timeout |
| N+1 query | `GenerateQuote` fetches all cart items in one `WHERE id IN (?)` query |
| Mapbox cache | Coordinate-quantized distance cache, evicted hourly |
| Catalog cache | In-memory cache invalidated only on admin writes |
| gRPC server | `LimitListener(512)`, `MaxConcurrentStreams(256)`, keepalive |
| gRPC client | 8-channel pool in the bot with keepalive options |
| Cart writes | Buffered in bot memory, flushed lazily after 5 s idle |
| Order index | `idx_orders_status_ts` makes pending-order queries O(log n) |
| Quote debounce | 2 s per-user cooldown prevents Mapbox API storms |
| Stats collector | Background goroutine; TUI reads `atomic.Value` — never blocks on Docker |
| Pricing | `baseFare`, `ratePerKm`, `taxRate` configurable via config table at runtime |
| Soft deletes | Removed items kept in DB for historical receipt integrity |
| Audit log | All admin mutations written to `audit_log` table |
