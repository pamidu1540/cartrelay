# 🚚 Logistics Engine

A high-performance, containerized logistics and e-commerce platform built with Go, Python, and gRPC. It provides a Telegram-based storefront for customers and a robust CLI supervisor for zero-downtime server management.

## 🏗️ Architecture

The system is composed of three main microservices/components, orchestrated via Docker Compose:

1. **Engine (Go / SQLite)**
   The core backend service. It exposes a highly-performant gRPC API over a Unix Domain Socket (`/app/sockets/logistics.sock`). It handles:
   - Atomic transactions for inventory management.
   - Idempotent order processing and cart state persistence.
   - Background tasks like purging abandoned carts.
   - Dynamic map-based distance and pricing calculations via Mapbox API.

2. **Bot (Python / python-telegram-bot)**
   The user-facing frontend. It communicates with the Engine via gRPC.
   - **Customer Flow**: Browse the catalog, add items to a cart, share a delivery location, get live delivery quotes, and place orders.
   - **Admin Flow**: A dedicated `/admin` dashboard for managing the catalog, updating pricing/warehouse coordinates, viewing pending orders, and dispatching/canceling shipments.

3. **Supervisor CLI (Go)**
   A compiled `logistics` binary installed on the host machine (`/usr/local/bin/logistics`). It acts as a wrapper around Docker Compose and provides:
   - **OTA Updates**: `logistics update` securely pulls cryptographically signed tarballs from Cloudflare R2, extracts them, and restarts the stack with zero downtime.
   - **Rollbacks**: `logistics rollback` reverts to the previous version.
   - **Monitoring**: `logistics dashboard` (TUI) and `logistics logs` for easy host management.

## 🚀 Deployment & CI/CD

This repository features a fully automated, hands-off continuous integration and deployment pipeline powered by **GitHub Actions**:

- **CI (`ci.yml`)**: On every push, GitHub Actions automatically runs `protoc` and `go mod tidy` and commits the generated code back to `main`. It then runs `go test`, `go vet`, `ruff`, and `govulncheck` before building the Docker images and pushing them to the GitHub Container Registry (`ghcr.io`).
- **Release (`release.yml`)**: Triggered by pushing a version tag (e.g., `v5.1.1`). It builds the Go binaries for the Supervisor and Engine, builds the Docker images, signs the OTA update payload using Ed25519, and pushes all artifacts to a **Cloudflare R2** bucket.
- **Security**: Includes `codeql.yml` for weekly static analysis, and `dependency-review.yml` to block PRs that introduce vulnerable packages.

## 🛠️ Installation

The system is designed to be installed on a fresh Ubuntu/Debian server using the provided `install.sh` script.

```bash
sudo bash install.sh
```

**What the installer does:**
1. Checks system requirements (RAM, Disk).
2. Installs Docker.
3. Prompts for a License Key and validates it against the Cloudflare Workers License API.
4. Prompts for Telegram Bot Token and Admin IDs.
5. Generates internal security tokens and the `docker-compose.yml`.
6. Sets up a `systemd` service (`logistics.service`) for auto-restart on boot.
7. Pulls the latest images and starts the stack.

## 💻 Development Setup

To run the stack locally for development:

1. Copy `.env.example` to `.env` and fill in your Bot Token and Admin ID.
2. Generate the gRPC protobuf files (if making changes to `logistics.proto`):
   *(Note: CI handles this automatically when pushing to GitHub)*
   ```bash
   # Engine
   protoc --proto_path=engine/api --go_out=engine/api --go_opt=paths=source_relative --go-grpc_out=engine/api --go-grpc_opt=paths=source_relative engine/api/logistics.proto
   
   # Bot
   python -m grpc_tools.protoc -Iengine/api --python_out=bot/src/api --grpc_python_out=bot/src/api engine/api/logistics.proto
   ```
3. Run the stack using Docker Compose:
   ```bash
   docker compose up --build
   ```

## 🔒 Security Posture

- **Internal Communication**: The Bot and Engine communicate exclusively via a shared Unix Domain Socket mounted in a Docker volume. No internal ports are exposed to the host network.
- **Authentication**: gRPC calls are secured via an `INTERNAL_TOKEN` passed in the metadata.
- **OTA Verification**: Updates are signed using Ed25519 cryptographic signatures to prevent tampering via MITM attacks on the CDN.

## 📂 Directory Structure

- `/engine/` - Go backend, gRPC server, SQLite database logic.
- `/bot/` - Python Telegram Bot, gRPC client, UI handlers.
- `/supervisor/` - Go CLI for managing the deployed stack.
- `/.github/workflows/` - CI/CD pipelines.
- `install.sh` - Host installation script.
