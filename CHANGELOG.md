# Changelog

All notable changes to CartRelay (formerly Logistics Engine Core) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed
- Renamed the product and repository to CartRelay, reflecting its Telegram ecommerce workflow.
- Rewrote setup, architecture, deployment, contribution, and security documentation with explicit feature boundaries.
- Simplified Linux installation to a source build with encrypted configuration and preserved deployment identifiers.
- CI validates pull requests without pushing generated commits back to main.

### Fixed
- Host CLI maintenance socket path and encrypted configuration handling.
- Scoped container environments, removed misleading live-support/privacy promises, and isolated database tests behind TEST_DATABASE_URL.
- OTA signing reads its private key from the environment rather than command arguments.

## [v12.8.0] - 2026-09-06

### ⚡ Message Handling & Initial Step Optimizations
- **Zero-Flicker SMI Transitions:** Optimized `send_or_edit_main_menu` to run previous message deletion concurrently in the background (`asyncio.create_task`), eliminating UI blank screen delays and halving message transition latency.
- **Smart Reply Keyboard Handling:** Added instant detection for `ReplyKeyboardMarkup` and `ReplyKeyboardRemove` to bypass failed `edit_message_text` network attempts and streamline custom keyboard rendering.
- **Sub-100ms `/start` Performance:** Cached warehouse and catalog setup verification (60s TTL) and onboarded status in memory to bypass redundant gRPC calls for returning users.
- **Streamlined Onboarding Wizard:** Staged user profile data in memory to eliminate intermediate gRPC roundtrips, and replaced flashing dummy keyboard cleaning messages with clean `ReplyKeyboardRemove()`.
- **Cached Catalog & Checkout Queries:** Cached warehouse verification for catalog commands; cached user address selections to eliminate redundant `ListSavedAddresses` gRPC calls; parallelized quote calculation and loyalty balance lookup via `asyncio.gather`.

### 🛡️ Bot Hardening & Telegram API Fixes
- **Telegram `query.answer` Duplicate Call Bug Fix:** Removed redundant initial empty `query.answer()` calls before alert toasts across callback handlers in `management.py`, `profile.py`, `checkout.py`, `orders.py`, and `cart.py`, preventing Telegram `BadRequest: Query is too old or query id is invalid` errors.
- **Non-Text Input Safety Guards:** Guarded against `None` message text across all wizard input states (`name`, `price`, `stock`, `category`, `description`, `promo_code`, `broadcast`, `driver`, `warehouse`, `settings`), preventing unhandled `AttributeError` exceptions when attachments, stickers, or photos are received.

### ⚙️ Engine & Supervisor Bug Fixes
- **Scheduled Maintenance Window Enforcement:** Fixed premature activation of scheduled maintenance by storing and enforcing both `maintenance_from` and `maintenance_until` boundaries.
- **Notification Spam Loop Remediation:** Wrapped `pending_notifs` retrieval in a row-level locking transaction (`FOR UPDATE`) and batch updated `sent = TRUE`, eliminating the 30-second infinite resend loop.
- **Auto-Dispatch Query Fix:** Fixed SQL column reference from `id` to `order_id` in `orders` table existence checks.
- **Saved Address Deduplication:** Added explicit update vs. insert branching in `DBSaveAddress` to prevent duplicate row creation on edits.
- **Catalog Date Window & Active Checks:** Enforced active item status and date windows in `CalculateQuote` and `FetchCatalogItems`.
- **Birthday Voucher Deduplication:** Added current calendar year verification to prevent duplicate birthday voucher assignments on engine restarts.
- **Supervisor OTA Root Extraction:** Resolved tarball extraction failure when encountering `.` root path entries.

### 📦 System Stack & Package Release
- **Python 3.13 Runtime:** Upgraded Python stack to 3.13 across bot service, Dockerfiles, pyproject.toml, and CI workflows.
- **Package Release:** Built and released `logistics-bot` package version `12.8.0`.

---

## [v12.7.0] - 2026-07-26

### 🛡️ Full-Stack Security Remediations
- **Rate Limiter DoS Protection (`HIGH-1`, `HIGH-2`):** Updated `RateLimitInterceptor` to route anonymous requests (`actorID == 0`) through a global bucket (60 req/min) and capped `rateBuckets` map at 100,000 entries (`maxRateBuckets`) to prevent memory exhaustion DoS attacks.
- **Secret Masking (`HIGH-3`):** Updated `tokenPrefix()` in `engine/main.go` to mask secret material, logging only the last 4 characters (`"…" + t[len(t)-4:]`).
- **Catalog Cache Stampede Prevention (`HIGH-4`):** Replaced full catalog cache wipes with an expired-first, oldest-half LRU purge strategy when reaching `catalogMaxItems`.
- **AES-256-GCM Encryption (`CRIT-2`, `MED-3`):** Updated `getAESKey()` and `decryptEnvFile()` in `supervisor/chunked_gcm.go` to use the full 32-byte SHA-256 hash for true AES-256-GCM stream encryption.
- **HMAC Window & Auth Hardening (`MED-1`, `MED-5`):** Reduced HMAC request clock skew window from $\pm 5\text{s}$ to $\pm 2\text{s}$ ($5$ hashes max per request) and updated Python bot `make_auth_token()` to read `INTERNAL_TOKEN` dynamically via `os.getenv()`.
- **Clock Skew Resilience (`MED-4`):** Switched Python bot admin cache and maintenance TTL checks to `time.monotonic()` to guard against NTP clock shifts.

### 🤖 Subagent Parallel Verification
- **Multi-Layer Audit:** Deployed specialized subagents (`engine-verifier`, `supervisor-verifier`, `bot-verifier`) to verify parallel stack integrity.
- **Test & Linter Pass Rate:** 100% pass rate across Go unit tests, Supervisor chunked crypto tests, 12/12 Python `pytest` tests, `golangci-lint`, and `ruff`.

---

## [v12.6.0] - 2026-07-25

### 🛡 Proprietary Licensing & Security
- **Software License:** Updated repository software license to **Proprietary Software License Agreement (All Rights Reserved)**.
- **Dependency Hardening:** Upgraded 80+ Go dependencies across `engine` and `supervisor` modules (`pgx/v5`, `google.golang.org/grpc`, `opentelemetry`, `zap`, `bubbletea`, `moby/client`).
- **Python Environment:** Synchronized Python virtual environment (`.venv`) via `uv`.

### 🧪 Testing & Benchmarks
- **Go Engine Testing Framework:** Created `routing_math_test.go`, `catalog_cache_test.go`, `interceptors_test.go`, and `license_verifier_test.go`. Refactored `TestMain` so 0-database unit tests execute unconditionally while DB integration tests skip gracefully.
- **Go Supervisor Testing Suite:** Created `chunked_gcm_test.go` with roundtrip AES-256-GCM chunked stream encryption/decryption tests.
- **Python Bot Suite:** Modularized `pytest` test suite with `conftest.py` shared fixtures.
- **Full-Scale Benchmark Suite:** Implemented `engine_benchmark_test.go` and `supervisor_benchmark_test.go`:
  - `BenchmarkHaversineKM`: **21.5M ops/sec** (46.4 ns/op, **0 B/op**, 0 allocs/op)
  - `BenchmarkMapboxCacheKey`: **7.8M ops/sec** (148.9 ns/op)
  - `BenchmarkMapboxCache_SetAndGet`: **3.1M ops/sec** (395.9 ns/op)
  - `BenchmarkMapboxCache_HighContention`: **2.2M ops/sec** under 12-core parallel worker lock contention
  - `BenchmarkValidateHMACToken`: **1.0M ops/sec** (3.3 μs/op)
  - `BenchmarkRecoveryInterceptor`: **263.5M ops/sec** (4.3 ns/op, **0 B/op**)
  - `BenchmarkChunkedGCM_Encrypt`: **1,587.74 MB/s** (41.2 μs / 64KB chunk)
  - `BenchmarkChunkedGCM_Decrypt`: **933.98 MB/s** (70.1 μs / 64KB chunk)

---

## [v12.5.7] - 2026-06-12

### 🐛 Permissions & Volume Mount Fixes
- **Host Path Pre-Creation:** Fixed an issue where the absolute host path for the Unix socket was not automatically created by Docker if it didn't exist before the supervisor attempted to `chown` it.
- **Permissions Auto-Healing:** Added root socket permissions helper container for non-root Unix Domain Socket execution.
