# Logistics Engine

A self-hosted logistics and commerce backend with a Telegram storefront. The engine and supervisor are written in Go; the Telegram bot is written in Python. Docker Compose runs PostgreSQL, the gRPC engine, and the bot.

## Components

- `engine/`: gRPC API, PostgreSQL migrations, order and catalog logic, and delivery distance calculation.
- `bot/`: Telegram customer and admin flows. It connects to the engine over a shared Unix socket by default.
- `supervisor/`: `logistics` CLI for Compose operations, status, logs, maintenance, and signed update commands.
- `deploy/`: production Compose file, systemd unit, and environment template.
- `local_test/`: local Compose stack and local environment template.

The gRPC socket is shared only between the engine and bot containers. The engine requires `INTERNAL_TOKEN` and `DATABASE_URL`; requests are authenticated with HMAC. Admin IDs can be seeded with `ADMIN_USER_IDS`.

## Local development

Requirements: Docker Compose, Go 1.25+, Python 3.13+, and Task. On Windows, initialize the bundled development tools with:

```powershell
.\init-dev.ps1
```

Then create `local_test/.env` (or run `task env`) and start the stack:

```powershell
task up
```

Useful commands:

- `task down` stops the local stack.
- `task lint` runs Go, Python, and Dockerfile linters.
- `task test` runs Go and Python unit tests.
- `task build` builds the engine and supervisor binaries.

The local bot needs a valid Telegram bot token to receive Telegram updates. Configure a Mapbox token only if you want driving-distance quotes; the engine has a Haversine fallback.

## Production deployment

On a fresh Ubuntu or Debian host, run:

```bash
sudo bash install.sh
```

The installer creates the production environment and Compose configuration, downloads the supervisor and images, and installs a systemd service. For a manual Compose setup, copy `deploy/.env.example` to `deploy/.env`, replace its required values, then run `docker compose -f deploy/docker-compose.yml up -d` from the repository root.

The release workflow publishes Linux amd64 binaries, Docker images, and a signed OTA manifest to GitHub Releases. The supervisor's update command uses `UPDATE_MANIFEST_URL`; update signature verification requires the matching public key configured in `supervisor/ota_updater.go`.

## License

Released under the MIT License. See [LICENSE](LICENSE).
