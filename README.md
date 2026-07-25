# 🚚 Logistics Engine

[![CI Status](https://img.shields.io/github/actions/workflow/status/pamidu1540/logistics-engine-core/ci.yml?branch=main&label=CI&logo=github)](https://github.com/pamidu1540/logistics-engine-core/actions)
[![Latest Tag](https://img.shields.io/github/v/tag/pamidu1540/logistics-engine-core?color=blue&label=Version&logo=github)](https://github.com/pamidu1540/logistics-engine-core/tags)
[![Go Version](https://img.shields.io/badge/Go-1.25.0-00ADD8?logo=go&logoColor=white)](https://go.dev/)
[![Python Version](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)

A high-performance, containerized logistics and e-commerce platform built with Go, Python, and gRPC. It provides a Telegram-based storefront for customers, a robust CLI supervisor for zero-downtime server management, and enterprise-grade Datadog APM tracing.

---

## 🏗️ Architecture

The system is composed of several microservices orchestrated via Docker Compose:

1. **Engine (Go 1.25 / PostgreSQL)**
   The core backend service exposing a high-performance gRPC API over a Unix Domain Socket (`/app/sockets/logistics.sock`). Features:
   - Atomic PostgreSQL transactions for inventory and order management.
   - Idempotent order processing and cart state persistence.
   - Dynamic Mapbox driving distance and Haversine fallback calculations.
   - **Performance Optimizations:** `singleflight` request coalescing, `errgroup` concurrency, and LRU spatial caching.

2. **Bot (Python 3.11+ / python-telegram-bot)**
   The user-facing Telegram frontend communicating with the Engine via gRPC:
   - **Customer Flow:** Browse catalog, add items to cart, share GPS locations, view dynamic quotes, place orders.
   - **Admin Flow:** Manage catalog items, update stock/pricing, assign drivers, resolve order issues.
   - **SMI UX:** Single Message Interface auto-cleans old messages for an app-like experience.

3. **Gateway (Python / FastAPI)**
   FastAPI web gateway acting as an HTTP-to-gRPC bridge for web applications.

4. **Supervisor CLI (Go 1.25)**
   Host-installed `logistics` CLI binary providing:
   - **Zero-Downtime OTA Updates:** Cryptographically signed updates pulled from CDN with Ed25519 verification.
   - **Rollbacks & Management:** One-command rollbacks, interactive TUI monitoring, and log streaming.

---

## ⚡ Performance Benchmarks

| Metric | Throughput | Latency | Memory / Allocs |
|---|---|---|---|
| **Haversine Distance Math** | **21,587,586 ops/sec** | `46.43 ns/op` | **`0 B/op`** (0 allocs) |
| **Spatial LRU Cache Read/Write** | **3,139,838 ops/sec** | `395.90 ns/op` | `64 B/op` (2 allocs) |
| **LRU Cache (12-Core Contention)** | **2,213,353 ops/sec** | `552.90 ns/op` | `64 B/op` (2 allocs) |
| **HMAC-SHA256 RPC Security** | **1,000,000 ops/sec** | `3.34 μs/op` | `2,438 B/op` |
| **Panic Recovery Interceptor** | **263,542,574 ops/sec** | `4.30 ns/op` | **`0 B/op`** (0 allocs) |
| **AES-256-GCM OTA Encryption** | **`1,587.74 MB/s`** | `41.27 μs / 64KB` | `214,514 B/op` |
| **AES-256-GCM OTA Decryption** | **`933.98 MB/s`** | `70.16 μs / 64KB` | `344,372 B/op` |

---

## 📜 Changelog Summary

For detailed revision history, see [CHANGELOG.md](CHANGELOG.md).

### 🚀 Recent Releases

#### [v12.6.0] - 2026-07-25
- **Dependencies:** Upgraded 80+ Go libraries across `engine` and `supervisor` modules (`pgx/v5`, `grpc`, `datadog-agent`, `bubbletea`, `moby`).
- **Testing Framework:** Created multi-layer unit test suite (`routing_math_test.go`, `catalog_cache_test.go`, `interceptors_test.go`, `chunked_gcm_test.go`, `conftest.py`) and full-scale Go benchmark suite.
- **Security & Quality:** Fixed `errcheck` resource closures across all modules, added Go doc comments for 80+ exported symbols, and enforced zero-warning `golangci-lint` and `ruff` pass rates.
- **CI/CD Pipeline:** Upgraded GitHub Actions `ci.yml` & `release.yml` with Hadolint Dockerfile static analysis and Go 1.25 check-latest workflows.

#### [v12.5.7] - 2026-06-12
- **Permissions:** Auto-healing root socket permissions helper container for non-root Unix Domain Socket execution.

---

## 🚀 Deployment & CI/CD

- **CI (`ci.yml`)**: On every push to `main`, GitHub Actions automatically tidy Go modules, run `protoc`, execute unit tests, perform `ruff` & `hadolint` linting, and publish multi-stage Docker images to `ghcr.io`.
- **Release (`release.yml`)**: Triggered by pushing a version tag (e.g., `v12.6.0`). Compiles Linux binaries, signs OTA payloads with Ed25519, and pushes release assets to Cloudflare R2 CDN.

---

## 🛠️ Installation

Install on a fresh Ubuntu/Debian server:

```bash
sudo bash install.sh
```

**Installer steps:**
1. Validates RAM & disk requirements.
2. Installs Docker and Docker Compose.
3. Prompts for License Key, Telegram Bot Token, Admin User IDs, and Datadog API key.
4. Generates encrypted environment configuration and sets up `logistics.service` `systemd` unit.

---

## 💻 Local Development

We use a cross-platform `Taskfile.yml` setup isolated inside a `.tools/` directory.

### Bootstrap Toolchain
```powershell
.\init-dev.ps1
```

### Task Commands
- `task env` - Interactively generate local `.env` configuration.
- `task up` - Start the Docker Compose stack.
- `task down` - Stop the local stack.
- `task lint` - Run Go (`golangci-lint`), Python (`ruff`), and Docker (`hadolint`) static analysis.
- `task test` - Run full Go and Python unit test suites.
- `task build` - Compile Engine and Supervisor binaries.

---

## 🔒 Security Posture

- **Unix Domain Socket Isolation**: Engine and Bot communicate over an internal socket; no public gRPC ports are exposed.
- **HMAC Request Validation**: Shared token HMAC-SHA256 request signing with $\pm 5\text{s}$ replay protection.
- **Hardware-Bound Secrets**: Environment variables encrypted at rest using machine hardware identifiers.

---

## 📂 Directory Structure

- `/engine/` - Go backend gRPC server, PostgreSQL database layer, routing math.
- `/supervisor/` - Go CLI supervisor for OTA updates, TUI dashboard, AES-256-GCM chunked crypto.
- `/bot/` - Python Telegram Bot, SMI handlers, cart manager, rate limiter.
- `/webui/gateway/` - FastAPI Web UI bridge.
- `/.github/workflows/` - CI/CD automated pipelines.

---

## 📄 License

This software is **Proprietary and Confidential**. All Rights Reserved. See [LICENSE](LICENSE) for details.
