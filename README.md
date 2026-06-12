# 🚚 Logistics Engine

A high-performance, containerized logistics and e-commerce platform built with Go, Python, and gRPC. It provides a Telegram-based storefront for customers, a robust CLI supervisor for zero-downtime server management, and enterprise-grade Datadog APM tracing.

## 🏗️ Architecture

The system is composed of several microservices orchestrated via Docker Compose:

1. **Engine (Go / PostgreSQL)**
   The core backend service. It exposes a highly-performant gRPC API over a Unix Domain Socket (`/app/sockets/logistics.sock`). It handles:
   - Atomic transactions for inventory management.
   - Idempotent order processing and cart state persistence.
   - Dynamic map-based distance and pricing calculations via Mapbox API.
   - **Performance Optimizations:** Utilizes `errgroup` for high-concurrency RPC processing, `singleflight` request coalescing, and LRU caching for 0-latency catalog loads and Mapbox routing.

2. **Bot (Python / python-telegram-bot)**
   The user-facing frontend communicating with the Engine via gRPC. 
   - **Customer Flow**: Browse the catalog, add items to a cart, natively share GPS delivery locations via Telegram, get live delivery quotes, and place orders.
   - **Admin Flow**: A dedicated dashboard for managing the catalog, updating pricing/warehouse coordinates, viewing pending orders, and dispatching/canceling shipments.
   - **SMI UX**: Built using a **Single Message Interface (SMI)**. The bot natively manages, edits, and self-cleans old messages to keep the chat history completely pristine and app-like.

3. **Gateway (Python / FastAPI)**
   A high-speed FastAPI gateway designed to serve the Web UI frontend and act as an HTTP-to-gRPC bridge for future web applications.

4. **Supervisor CLI (Go)**
   A compiled `logistics` binary installed on the host machine (`/usr/local/bin/logistics`). It provides:
   - **OTA Updates**: `logistics update` securely pulls cryptographically signed tarballs from Cloudflare R2, extracts them, and restarts the stack with zero downtime.
   - **Rollbacks**: `logistics rollback` reverts to the previous version.
   - **Monitoring**: `logistics dashboard` (TUI) and `logistics logs`.

## 🚀 Deployment & CI/CD

This repository features a fully automated continuous integration and deployment pipeline powered by **GitHub Actions**:

- **CI (`ci.yml`)**: On every push, GitHub Actions automatically runs `protoc` and `go mod tidy` and commits the generated code back to `main`. It then runs Go tests, `ruff` linting, and `govulncheck` before building the Docker images and pushing them to the GitHub Container Registry (`ghcr.io`).
- **Release (`release.yml`)**: Triggered by pushing a version tag (e.g., `v12.5.2`). Builds the Go binaries, Docker images, signs the OTA update payload using Ed25519, and pushes all artifacts to a **Cloudflare R2** bucket.
- **Security**: Includes `codeql.yml` for static analysis and `go-security.yml` to block vulnerabilities.

## 🛠️ Installation

The system is designed to be installed on a fresh Ubuntu/Debian server using the provided `install.sh` script.

```bash
sudo bash install.sh
```

**What the installer does:**
1. Checks system requirements (RAM, Disk).
2. Installs Docker.
3. Prompts for a License Key and validates it against the Cloudflare Workers License API.
4. Prompts for Telegram Bot Token, Admin IDs, and Datadog API keys.
5. Generates internal security tokens and the `docker-compose.yml`.
6. Sets up a `systemd` service (`logistics.service`) for auto-restart on boot.
7. Pulls the latest images and starts the stack.

## 💻 Development Setup

We use a fully automated, cross-platform `Taskfile.yml` framework to guarantee identical CI/CD workflows both locally and on GitHub Actions, isolated inside a hidden `.tools/` directory to prevent global OS pollution.

### 1. Bootstrap the Toolchain
On Windows, run the initialization script to compile the task runner and bootstrap all dependencies (`uv`, `golangci-lint`, `gosec`, `ruff`, `pytest`) into an isolated virtual environment:
```powershell
.\init-dev.ps1
```
*(This automatically adds `.tools\bin` to your session PATH).*

### 2. Orchestration Commands
- `task env` - Interactively generates your `local_test/.env` configuration.
- `task up` - Starts the full Docker Compose stack with Datadog locally.
- `task down` - Stops the local stack.
- `task lint` - Runs Go and Python static analysis.
- `task test` - Runs Go and Python unit tests.
- `task build` - Compiles the `engine` and `supervisor` binaries.

## 🔒 Security Posture

- **Internal Communication**: The Bot and Engine communicate exclusively via a shared Unix Domain Socket mounted in a Docker volume. No internal ports are exposed to the host network.
- **Authentication**: gRPC calls are secured via an `INTERNAL_TOKEN` passed in the metadata.
- **OTA Verification**: Updates are signed using Ed25519 cryptographic signatures to prevent tampering via MITM attacks on the CDN.
- **Hardware Encryption**: The `.env` variables are completely encrypted and securely decrypted in-memory using a hardware-bound key (`/etc/machine-id`) to mitigate disk theft.

## 📊 Observability
The stack integrates natively with **Datadog** via a dedicated `datadog-agent` sidecar. This provides out-of-the-box Continuous Profiling for the Go Engine, Distributed APM Tracing across the gRPC boundary, and full Postgres query profiling.

## 📂 Directory Structure

- `/engine/` - Go backend, gRPC server, PostgreSQL database logic.
- `/bot/` - Python Telegram Bot, gRPC client, UI handlers.
- `/supervisor/` - Go CLI for managing the deployed stack.
- `/webui/gateway/` - FastAPI backend for web frontend.
- `/.github/workflows/` - CI/CD pipelines.
- `install.sh` - Host installation script.
