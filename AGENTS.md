# 🤖 AGENTS.md — Agent Guidelines & Repository Context for Google Jules

Welcome to **Logistics Engine Core**. This document serves as the primary instructions and architecture context for **Google Jules** (and other AI coding agents) operating within this repository.

---

## 🏗️ System Architecture & Overview

The **Logistics Engine** is a high-performance, containerized logistics and e-commerce platform built with **Go**, **Python**, **FastAPI**, and **gRPC**.

### Microservices Structure
1. **Engine (`/engine`)** — *Go / PostgreSQL / gRPC*
   - Core backend domain logic running a gRPC server listening on Unix Domain Socket (`/app/sockets/logistics.sock`).
   - High-concurrency transaction handling, inventory management, Mapbox routing math, `singleflight` request coalescing, and LRU catalog caching.
2. **Bot (`/bot`)** — *Python 3.12 / python-telegram-bot / gRPC Client*
   - User-facing storefront and admin dashboard using a **Single Message Interface (SMI)**.
   - Communicates with the Engine exclusively via gRPC over the shared Unix socket.
3. **Supervisor CLI (`/supervisor`)** — *Go*
   - Host management tool (`logistics` binary).
   - Handles zero-downtime OTA updates via signed tarballs (Ed25519), rollback logic, docker-compose orchestration, and TUI dashboards.
4. **Gateway (`/webui/gateway`)** — *FastAPI / Python*
   - REST/HTTP-to-gRPC bridge serving web clients and external API endpoints.

---

## ⚙️ Google Jules VM Setup Instructions

Google Jules runs tasks inside isolated, short-lived virtual machines. To initialize the environment correctly for testing and linting, ensure the setup script is executed.

### Initial Setup Script
When configuring Jules in the UI, set the **Initial Setup** command to:
```bash
bash .jules/setup.sh
```

### Manual Setup Steps (if running custom commands)
- **Go Version:** Go `1.25+`
- **Python Version:** Python `3.12+` (managed via `uv`)
- **Protobuf Compiler:** `protobuf-compiler` (`protoc`)
- **Go Plugins:** `protoc-gen-go`, `protoc-gen-go-grpc`
- **Python gRPC Tools:** `grpcio-tools==1.80.0`

---

## 🛠️ Build, Test & Lint Workflows

We use `Taskfile.yml` for unified development workflows across OS environments.

### Task Commands
- `task lint` — Runs `golangci-lint` on `/engine` & `/supervisor`, and `ruff` on `/bot`.
- `task test` — Runs Go unit tests (`go test -v ./...`) and Python unit tests (`pytest`).
- `task build` — Compiles the `engine` and `supervisor` Go binaries into `/bin/`.

### Direct Tool Commands

#### Go (Engine & Supervisor)
```bash
# Engine lint & test
cd engine
golangci-lint run ./...
go test -v -race ./...
govulncheck ./...

# Supervisor lint & test
cd supervisor
golangci-lint run ./...
go test -v -race ./...
govulncheck ./...
```

#### Python (Bot)
```bash
cd bot
uv run ruff check src/
uv run pytest tests/
```

---

## 📜 Protobuf Code Generation Rules

> ⚠️ **CRITICAL RULE:** Never directly modify auto-generated gRPC code.

All gRPC communication contracts are defined in `engine/api/logistics.proto`.

### Re-generating Protobuf Bindings
If you modify `engine/api/logistics.proto`, re-generate the bindings for both Go and Python:

```bash
# Go bindings
protoc --proto_path=engine/api \
  --go_out=engine/api --go_opt=paths=source_relative \
  --go-grpc_out=engine/api --go-grpc_opt=paths=source_relative \
  engine/api/logistics.proto

# Python bindings
mkdir -p bot/src/api
python3 -m grpc_tools.protoc \
  -Iengine/api \
  --python_out=bot/src/api \
  --grpc_python_out=bot/src/api \
  engine/api/logistics.proto

# Fix Python relative import
sed -i 's/^import logistics_pb2/from . import logistics_pb2/' bot/src/api/logistics_pb2_grpc.py
```

*Note: In CI/CD (`ci.yml`), `protoc` code generation runs automatically on push to `main` and commits changes back.*

---

## 🔒 Security & Architectural Rules

1. **IPC Security:** The Bot and Engine must communicate over Unix Domain Sockets (`/app/sockets/logistics.sock`). Never expose raw gRPC ports directly to the public network.
2. **Metadata Auth:** All gRPC requests must include the `INTERNAL_TOKEN` in gRPC metadata.
3. **OTA Integrity:** Any updates in `supervisor/ota_updater.go` must verify Ed25519 signatures before extracting payloads.
4. **Environment Isolation:** Do not hardcode secret keys or tokens. Use `.env` or system environment variables (`TELEGRAM_BOT_TOKEN`, `INTERNAL_TOKEN`, `POSTGRES_PASSWORD`).

---

## 📋 Checklist for Jules Agent Tasks

Before submitting a Pull Request or completing a task:
- [ ] Code passes all tests (`task test` or `go test` / `pytest`).
- [ ] Code passes linting (`task lint` or `golangci-lint` / `ruff`).
- [ ] Security vulnerability checks pass (`govulncheck`).
- [ ] Protobuf definitions match between Go and Python bindings.
- [ ] Existing comments, docstrings, and architectural invariants are preserved.
