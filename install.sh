#!/usr/bin/env bash
# Install a reviewed source checkout. Existing deployments are never overwritten.
set -euo pipefail
umask 077

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/logistics-system
BIN_PATH=/usr/local/bin/cartrelay

die() { printf 'CartRelay: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die 'Run with sudo bash install.sh.'
[[ $(uname -m) == x86_64 ]] || die 'This installer supports Linux x64 only.'
[[ -f /etc/debian_version ]] || die 'This installer supports Ubuntu/Debian hosts.'
for tool in docker go python3 openssl systemctl tar; do
  command -v "$tool" >/dev/null || die "Missing prerequisite: $tool"
done
docker compose version >/dev/null
docker info >/dev/null
python3 -c 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM' || die 'Install python3-cryptography first.'
[[ ! -e "$INSTALL_DIR/deploy/docker-compose.yml" && ! -e "$INSTALL_DIR/deploy/.env" && ! -e "$INSTALL_DIR/deploy/.env.enc" ]] || die 'Existing deployment found. Follow docs/DEPLOYMENT.md to update it safely.'
[[ ! -e "$BIN_PATH" && ! -L "$BIN_PATH" && ! -e /usr/local/bin/logistics && ! -L /usr/local/bin/logistics ]] || die 'An installed CLI already exists; inspect it before installing.'
[[ ! -e /etc/systemd/system/logistics.service ]] || die 'An existing service must be reviewed before installing.'

printf 'CartRelay source installation\n'
read -rs -p 'Telegram bot token (hidden): ' TELEGRAM_BOT_TOKEN </dev/tty; printf '\n'
read -r -p 'Administrator Telegram IDs (comma separated): ' ADMIN_USER_IDS </dev/tty
read -rs -p 'Mapbox token (optional, hidden): ' MAPBOX_TOKEN </dev/tty; printf '\n'
[[ $TELEGRAM_BOT_TOKEN =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || die 'Invalid Telegram bot token format.'
[[ $ADMIN_USER_IDS =~ ^[0-9]+(,[0-9]+)*$ ]] || die 'Administrator IDs must be comma-separated positive numbers.'
[[ $MAPBOX_TOKEN != *$'\n'* && $MAPBOX_TOKEN != *$'\r'* ]] || die 'Invalid Mapbox token.'
INTERNAL_TOKEN=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 32)
POSTGRES_USER=logistics
POSTGRES_DB=logistics
APP_VERSION=source
export TELEGRAM_BOT_TOKEN ADMIN_USER_IDS MAPBOX_TOKEN INTERNAL_TOKEN POSTGRES_PASSWORD POSTGRES_USER POSTGRES_DB APP_VERSION

temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
printf 'Building the CartRelay CLI...\n'
(cd "$SOURCE_DIR/supervisor" && go build -trimpath -o "$temporary/cartrelay" .)

install -d -m 750 "$INSTALL_DIR" "$INSTALL_DIR/deploy"
install -d -m 755 "$INSTALL_DIR/deploy/sockets"
# Copy only source components; credentials and local build caches are excluded.
tar -C "$SOURCE_DIR" --exclude='.env' --exclude='.env.*' --exclude='.secrets' --exclude='*.pem' --exclude='*.key' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.log' --exclude='*.exe' -cf - engine bot |
  tar -C "$INSTALL_DIR" -xf -
install -m 644 "$SOURCE_DIR/deploy/docker-compose.yml" "$INSTALL_DIR/deploy/docker-compose.yml"

# Write only ciphertext. The supervisor uses the same full SHA-256 key derivation.
python3 - "$INSTALL_DIR/deploy/.env.enc" <<'PY'
import hashlib
import os
import sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

names = ("TELEGRAM_BOT_TOKEN", "ADMIN_USER_IDS", "MAPBOX_TOKEN", "INTERNAL_TOKEN",
         "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "APP_VERSION")
config = "\n".join(f"{name}={os.environ[name]}" for name in names)
config += "\nDEPLOY_ENV=production\nLOG_LEVEL=info\n"
machine_id = Path("/etc/machine-id").read_text().strip()
if not machine_id:
    raise SystemExit("Empty machine ID; refusing to write configuration")
key = hashlib.sha256(machine_id.encode()).digest()
nonce = os.urandom(12)
destination = Path(sys.argv[1])
with destination.open("xb") as output:
    output.write(nonce + AESGCM(key).encrypt(nonce, config.encode(), None))
destination.chmod(0o600)
PY

printf 'Building application images...\n'
docker compose -f "$INSTALL_DIR/deploy/docker-compose.yml" --env-file /dev/null build
install -m 755 "$temporary/cartrelay" "$BIN_PATH"
ln -s cartrelay /usr/local/bin/logistics
install -m 644 "$SOURCE_DIR/deploy/logistics.service" /etc/systemd/system/logistics.service
systemctl daemon-reload
systemctl enable --now logistics.service
printf '\nInstalled. Check sudo cartrelay status and sudo cartrelay logs.\nComplete store setup and a test order before accepting customers.\n'
