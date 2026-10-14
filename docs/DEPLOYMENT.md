# Deployment and operations

Test with a separate Telegram bot and customer account first. Configure warehouses, products, stock, tax, delivery fees, and administrator access. Exercise a complete order and notification flow. Payment collection must be arranged separately.

## Configuration

`deploy/.env.example` lists required settings. Optional `MAPBOX_TOKEN` enables driving distance; blank uses a straight-line approximation. Set pricing through the bot's store settings.

For direct Compose deployments, inject settings through your secret manager or a protected environment file outside the checkout. Use the same Compose file path and project name on upgrades so volumes are reused. Keep the host patched and restrict Docker access. No database or engine port is published by default.

## Source installer

The installer requires a fresh Ubuntu/Debian x64 host, systemd, Docker Compose, Go 1.25+, Python 3 with `cryptography`, OpenSSL, and tar. It builds images and the supervisor from this checkout; it does not install operating-system packages or download application binaries.

An existing deployment or configuration makes it stop before overwriting files. The installer keeps `/opt/logistics-system` and `logistics.service` for compatibility. It installs `cartrelay` with a `logistics` alias and uses local images. Configuration is encrypted in `/opt/logistics-system/deploy/.env.enc` with a key derived from `/etc/machine-id`. This prevents casual plaintext exposure; it does not protect against host administrators or a copied machine ID.

```bash
sudo cartrelay status
sudo cartrelay logs --service engine
sudo cartrelay logs --service bot
sudo cartrelay maintenance
sudo systemctl status logistics
```

## Backups and updates

Back up PostgreSQL with `pg_dump` and verify restoration into a disposable database. Preserve configuration, the machine ID for encrypted installs, and the source version. Restrict backup access: orders contain customer details.

Supervisor deployment snapshots are not PostgreSQL backups. They cannot reverse migrations or recover lost business data.

For direct Compose deployments, back up, review changes and migrations, update the checkout, then run the same Compose command with `up --build -d`. Check logs and a test purchase afterward.

The source installer does not overwrite existing installations. For an installed source deployment, rebuild the local images with the same tags from a reviewed checkout, rebuild the CLI if needed, and restart the service. Retain the previous images and a database backup until verification is complete.

Optional OTA tooling needs matching Ed25519 keys. Its public key is compiled into `supervisor/ota_updater.go`; the private key belongs only in the GitHub Actions secret `OTA_PRIVATE_KEY`. Forks need their own matching keys. Source installs do not enable OTA automatically. `update --dry-run` fetches metadata only; applying an update verifies its payload signature.

## Uninstall

Stop and disable `logistics.service`, then remove its service file and CLI aliases if no longer needed. Preserve `/opt/logistics-system` and Docker volumes until you have exported and checked all required data. Volume deletion permanently removes store data.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Bot silent | Token, outbound network, bot logs, another process polling the same bot |
| Engine unavailable | Database readiness/credentials, migrations, nonempty internal token |
| Access denied | Matching internal tokens and correct numeric admin ID |
| Empty catalog | Warehouse, available products, stock |
| Unexpected quote | Coordinates, Mapbox configuration, store pricing |
| Healthy socket but failed checkout | Application logs; socket existence is not an end-to-end check |
