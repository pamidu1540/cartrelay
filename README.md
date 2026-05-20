# Logistics Engine v4

A robust, microservice-based delivery management system built for Telegram. Customers can browse a catalog, get live delivery quotes via Mapbox routing, and place orders directly in-chat. Administrators have access to an interactive, in-chat lifecycle management panel.

```text
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

## 🏗️ Comprehensive Tech Stack

### 1. Backend Engine (Go)
- **Runtime**: Go 1.25.10
- **Architecture**: A high-performance gRPC server handling all core business logic, pricing, and stock management.
- **Database**: SQLite3. Highly tuned for concurrent access (WAL mode, `MaxOpenConns=1`, 64MB page cache, 256MB mmap, 5-second busy timeout). Includes an `audit_log` table for admin mutations.
- **External APIs**: Integrates with the **Mapbox API** to calculate precise delivery distances. Implements a coordinate-quantized cache (evicted hourly) and a 2-second per-user debounce to prevent API rate-limit storms.
- **Optimizations**: In-memory catalog caching (invalidated only on writes) and atomic stock decrements. 

### 2. Telegram Frontend Bot (Python)
- **Runtime**: Python 3.12
- **Framework**: `python-telegram-bot` (v21.3+)
- **gRPC Integration**: Connects to the engine via a Unix socket. To safely bridge synchronous `grpcio` calls into the bot's `asyncio` event loop without loop-binding errors, all RPCs are offloaded to a dedicated `ThreadPoolExecutor`.
- **State Management**: User cart states are buffered in memory and lazily flushed to the engine after 5 seconds of inactivity, drastically reducing SQLite write volume.

### 3. Supervisor & Deployment Tooling
- **Supervisor**: A standalone Go binary utilizing the Docker SDK and gRPC to monitor and manage the stack. Features an interactive Terminal UI (TUI) for reading logs, checking health, and triggering OTA (Over-The-Air) updates.
- **Containerization**: The engine and bot run in isolated Docker containers deployed via Docker Compose.

### 4. CI/CD & Infrastructure
- **GitHub Actions**: Fully automated pipelines that:
  - Auto-generate and commit protobuf bindings.
  - Run `go test`, `pytest`, `ruff`, and `govulncheck`.
  - Perform static analysis via CodeQL and block CVEs via Dependency Review.
- **Distribution**: 
  - Docker images are continuously published to **GHCR** (GitHub Container Registry).
  - Standalone Supervisor binaries are automatically uploaded to **Cloudflare R2** on release tags for seamless OTA rollouts.

---

## 🚀 Quick Start

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

## 🛠️ Supervisor CLI

The supervisor binary provides a suite of tools for managing the active node.

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
