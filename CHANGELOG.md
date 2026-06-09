# Changelog

All notable changes to this project will be documented in this file.

## [v12.0.0] - 2026-06-08

### 🚀 Major Performance Optimizations
- **Engine Concurrency:** Transitioned linear DB lookups into parallel execution utilizing `golang.org/x/sync/errgroup` to slash `CalculateQuote` latency.
- **LRU Caching:** Eliminated full catalog re-fetches for cart generation in Python, and added an $O(1)$ LRU cache in Go using `container/list` to prevent expensive map sorting.
- **Network Request Coalescing:** Implemented `singleflight` groups for concurrent identical Mapbox routing requests to prevent hitting rate-limits during burst loads.
- **Python Bot Tuning:**
  - Switched sequentially blocking RPCs in `user_handlers.py` to run concurrently with `asyncio.gather()`.
  - Prevented the `csv.DictReader` bulk importer from blocking the async event loop using `asyncio.to_thread()`.
  - Eliminated unbounded memory leaks by converting the in-memory cart buffer to an `OrderedDict`-based LRU cache with TTL eviction.
  - Refactored SSL certificate loading to load asynchronously and cache the certificate strictly once.

### ✨ Professional Telegram UX Overhaul
- **Single Message Interface (SMI):** The bot now edits a single persistent main menu message instead of sending new ones, completely removing conversation bloat.
- **Auto-Cleanup:** The bot instantly deletes user text inputs (like search queries and manual address entry) after processing to keep the chat history pristine.
- **Elegant Toasts:** Status alerts (like "Item added to cart") now appear as transient popup toasts instead of polluting the chat log.

### 🐛 Bug Fixes
- **Mapbox URL Escaping Bug:** Fixed a critical bug in `engine/routing_math.go` where coordinate commas were erroneously URL-escaped (`%2C`), which caused `400 Bad Request` errors from the Mapbox Directions API.
- **Mapbox Directionality Logic:** Reversed the distance calculation coordinates from `(user -> warehouse)` to `(warehouse -> user)` to respect actual one-way street mapping for delivery drivers.
- **Mapbox Cache Eviction Crash:** Resolved an unbounded memory-leak bug during cache eviction that prevented old routing entries from being correctly garbage-collected.
- **Location Sharing:** Standardized all location sharing workflows across customer checkout and admin onboarding to utilize Telegram's native `KeyboardButton(request_location=True)` API.
- **Database Closures:** Resolved critical `G104` unhandled DB batch closure bugs identified by `gosec`.

### 🛡 Security & CI/CD
- Completed rigorous audits resulting in clean, green builds across native Python `ruff` linting, Python `pytest` suites, `gosec` static security scanning, CodeQL, and Engine Go integration tests.
- **Hardware-Bound .env Encryption**: Replaced plaintext `deploy/.env` with `.env.enc`. The `supervisor` now dynamically decrypts configurations in-memory during startup using a key derived from the server's `/etc/machine-id`, nullifying disk-theft attacks.

### 💻 Developer Experience
- **Taskfile CI/CD Pipeline**: Deprecated fragmented PowerShell scripts (`setup-dev.ps1`) in favor of a robust `Taskfile.yml` framework. 
- **Toolchain Isolation**: `task setup` natively bootstraps `uv`, `golangci-lint`, and `gosec` securely into a `.tools/bin` directory to completely avoid global OS pollution.
- **AV Bypass**: Implemented `init-dev.ps1` to compile the `task` runner natively from source using Go, completely bypassing false-positive dropper heuristics from Windows Defender.
