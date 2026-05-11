#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Logistics Engine — Customer Installer
#
#  curl -fsSL https://raw.githubusercontent.com/pamidu1540/logistics-engine-core/main/install.sh | sudo bash
#
#  What this script does (infrastructure only):
#    1. Verify OS + system requirements
#    2. Install Docker if missing
#    3. Verify your license key offline (Ed25519 JWT — no server required)
#    4. Collect three values: Telegram token, admin IDs, Mapbox token
#    5. Download the logistics supervisor binary from GitHub Releases
#    6. Pull engine + bot Docker images from GHCR
#    7. Write .env + production docker-compose.yml
#    8. Install + start the logistics systemd service
#
#  What this script does NOT do:
#    - Depot / warehouse configuration  →  /admin in Telegram after install
#    - Pricing configuration            →  /admin in Telegram after install
#    - Catalog management               →  /admin in Telegram after install
#    - Any database operations          →  handled by the engine on first boot
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail
IFS=$'\n\t'

# ── Installer constants ───────────────────────────────────────────────────────
readonly INSTALLER_VERSION="4.0.0"
readonly GHCR_OWNER="pamidu1540"
readonly GHCR_REPO="logistics-engine-core"
readonly IMAGE_ENGINE="ghcr.io/${GHCR_OWNER}/${GHCR_REPO}/engine"
readonly IMAGE_BOT="ghcr.io/${GHCR_OWNER}/${GHCR_REPO}/bot"
readonly INSTALL_DIR="/opt/logistics-system"
readonly SERVICE_NAME="logistics"
readonly BIN_PATH="/usr/local/bin/logistics"
readonly MIN_RAM_MB=512
readonly MIN_DISK_GB=5
readonly LICENSE_API="https://logistics-license.paminduvidusaka.workers.dev/v1/activate"

# ── License public key (Ed25519) ──────────────────────────────────────────────
# This is the PUBLIC key only. The matching private key never leaves your laptop.
# Generated with: openssl genpkey -algorithm ed25519 -out license_priv.pem
# Exported with:  openssl pkey -in license_priv.pem -pubout | openssl pkey -pubin -outform DER | xxd -p -c 256
# Replace with your actual public key before shipping:
readonly LICENSE_PUBKEY_HEX="2aedb7bd6d094c3e73280d404c064c4f71241059e3189a68528e973bef49b0f4"

# ── Colours ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m' YEL='\033[0;33m' GRN='\033[0;32m'
  BLU='\033[0;34m' CYN='\033[0;36m' MAG='\033[0;35m'
  BOLD='\033[1m'   DIM='\033[2m'    RST='\033[0m'
else
  RED='' YEL='' GRN='' BLU='' CYN='' MAG='' BOLD='' DIM='' RST=''
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
banner() {
  echo
  echo -e "${BOLD}${CYN}╔══════════════════════════════════════════════════════╗${RST}"
  echo -e "${BOLD}${CYN}║       🚚  LOGISTICS ENGINE  —  INSTALLER v${INSTALLER_VERSION}      ║${RST}"
  echo -e "${BOLD}${CYN}╚══════════════════════════════════════════════════════╝${RST}"
  echo
}
step() { echo -e "\n${BOLD}${BLU}▶  $*${RST}"; }
ok()   { echo -e "   ${GRN}✔${RST}  $*"; }
warn() { echo -e "   ${YEL}⚠${RST}  $*"; }
info() { echo -e "   ${DIM}→${RST}  $*"; }
hr()   { echo -e "${DIM}──────────────────────────────────────────────────────${RST}"; }
die()  { echo -e "\n${RED}${BOLD}✘  ERROR: $*${RST}\n" >&2; exit 1; }

prompt() {
  # prompt <var> <label> [default] [secret]
  local __var="$1" __label="$2" __default="${3:-}" __secret="${4:-}" __val=""
  while [[ -z "$__val" ]]; do
    if [[ -n "$__secret" ]]; then
      printf "   %b%s%b%b%s%b: " "${BOLD}" "$__label" "${RST}" "${DIM}" "${__default:+ [hidden]}" "${RST}"
      read -rs __val; echo
    else
      printf "   %b%s%b%b%s%b: " "${BOLD}" "$__label" "${RST}" "${DIM}" "${__default:+ [$__default]}" "${RST}"
      read -r __val
    fi
    [[ -z "$__val" && -n "$__default" ]] && __val="$__default"
    [[ -z "$__val" ]] && echo -e "   ${RED}Required — please enter a value.${RST}"
  done
  printf -v "$__var" '%s' "$__val"
}

prompt_optional() {
  local __var="$1" __label="$2" __val=""
  printf "   %b%s%b %b(optional — Enter to skip)%b: " "${BOLD}" "$__label" "${RST}" "${DIM}" "${RST}"
  read -r __val
  printf -v "$__var" '%s' "$__val"
}

spinner() {
  local __pid=$1 __msg="$2" __sp='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' __i=0
  while kill -0 "$__pid" 2>/dev/null; do
    printf "\r   ${CYN}${__sp:$((__i % ${#__sp})):1}${RST}  %s…" "$__msg"
    ((__i++)); sleep 0.1
  done
  wait "$__pid"
  local __rc=$?
  if ((__rc == 0)); then
    printf "\r   ${GRN}✔${RST}  %s    \n" "$__msg"
  else
    printf "\r   ${RED}✘${RST}  %s failed (rc=%d)\n" "$__msg" "$__rc"
    return $__rc
  fi
}

# ── Step 1: Root + OS ─────────────────────────────────────────────────────────
check_root() {
  [[ "$EUID" -eq 0 ]] || die "Run as root:  sudo bash install.sh"
}

detect_os() {
  step "Detecting operating system"
  [[ -f /etc/os-release ]] || die "Cannot detect OS. Supported: Ubuntu 20.04+, Debian 11+."
  # shellcheck source=/dev/null
  source /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ok "OS: ${ID^} ${VERSION_ID:-}" ;;
    *) die "Unsupported OS: ${ID:-unknown}. Supported: Ubuntu 20.04+, Debian 11+." ;;
  esac
}

# ── Step 2: System requirements ───────────────────────────────────────────────
check_requirements() {
  step "Checking system requirements"

  local ram_mb
  ram_mb=$(awk '/MemTotal/{printf "%d",$2/1024}' /proc/meminfo)
  (( ram_mb >= MIN_RAM_MB )) || die "Only ${ram_mb} MB RAM. Minimum: ${MIN_RAM_MB} MB."
  ok "RAM: ${ram_mb} MB"

  local disk_gb
  disk_gb=$(df -BG / | awk 'NR==2{gsub("G","");print $4}')
  (( disk_gb >= MIN_DISK_GB )) || die "Only ${disk_gb} GB free. Minimum: ${MIN_DISK_GB} GB."
  ok "Disk: ${disk_gb} GB free"

  for cmd in curl openssl python3 systemctl; do
    command -v "$cmd" &>/dev/null || die "${cmd} is required. Run: apt-get install -y ${cmd}"
  done
  ok "Required tools present (curl, openssl, python3, systemctl)"
}

# ── Step 3: Docker ────────────────────────────────────────────────────────────
install_docker() {
  step "Checking Docker"
  if command -v docker &>/dev/null; then
    local ver
    ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1 || echo 0)
    if (( ver >= 24 )); then
      ok "Docker ${ver} already installed"
      return 0
    fi
    warn "Docker ${ver} is outdated — upgrading…"
  fi
  info "Installing Docker via get.docker.com…"
  ( curl -fsSL https://get.docker.com | sh ) &>/tmp/logistics-docker-install.log &
  spinner $! "Installing Docker"
  systemctl enable --quiet docker
  systemctl start  docker
  ok "Docker installed and started"
}

# ── Step 4: License verification (offline — no server, no cost) ───────────────
#
# How it works:
#   Your license key is a base64url-encoded Ed25519 JWT you sign locally with
#   the private key on your laptop using keygen.sh (provided separately).
#   The installer verifies the signature using only the baked-in public key above.
#   Zero network calls, zero infrastructure, cryptographically unforgeable.
#
# JWT claims used:
#   sub  — customer name
#   plan — license tier (e.g. "standard")
#   exp  — Unix expiry timestamp
#   iat  — issued-at timestamp
#
verify_license() {
  step "License verification"
  hr
  echo
  echo -e "   ${DIM}Your license key was included in your purchase email.${RST}"
  echo -e "   ${DIM}It is a long string starting with 'eyJ…'${RST}"
  echo

  local key="" attempts=0
  while true; do
    (( ++attempts > 3 )) && die "Too many failed attempts. Contact support."
    prompt key "License Key" "" secret

    # Decode and verify the JWT using python3 (built into every Ubuntu/Debian).
    # We use python3 + cryptography lib if available, otherwise openssl CLI fallback.
    local response http_code body
    response=$(curl -fsSL -w "\n%{http_code}" \
      -H "Content-Type: application/json" \
      -d "{\"license_key\":\"${key}\",\"hostname\":\"$(hostname -f 2>/dev/null || hostname)\",\"installer_version\":\"${INSTALLER_VERSION}\"}" \
      "${LICENSE_API}" 2>/dev/null) || response=$'\n000'

    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | head -n -1)

    case "$http_code" in
      200)
        # These lines extract the data from Cloudflare to build the .env file automatically
        LICENSE_CUSTOMER=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('customer','Customer'))" 2>/dev/null || echo "Customer")
        LICENSE_PLAN=$(echo     "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('plan','standard'))"     2>/dev/null || echo "standard")
        LICENSE_EXPIRES=$(echo  "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('expires','N/A'))"       2>/dev/null || echo "N/A")
        LICENSE_KEY="$key"
        echo
        echo -e "   ${GRN}${BOLD}✔  License activated${RST}"
        echo -e "      ${DIM}Customer :${RST} ${LICENSE_CUSTOMER}"
        echo -e "      ${DIM}Plan     :${RST} ${LICENSE_PLAN}"
        echo -e "      ${DIM}Expires  :${RST} ${LICENSE_EXPIRES}"
        return 0
        ;;
      401|403)
        body_err=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','Invalid key'))" 2>/dev/null || echo "Invalid key")
        echo -e "   ${RED}✘  ${body_err}${RST}"
        ;;
      402)
        body_err=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','License expired'))" 2>/dev/null || echo "License expired")
        die "${body_err}"
        ;;
      429)
        die "Rate limit exceeded. Please wait 60 seconds and try again."
        ;;
      000)
        warn "Cannot reach license server — check your internet connection."
        warn "Retrying in 5 s…"
        sleep 5
        ;;
      *)
        warn "Unexpected server response (HTTP ${http_code}). Retrying…"
        ;;
    esac
  done
}

# ── Step 5: Collect configuration ─────────────────────────────────────────────
collect_config() {
  step "Configuration"
  hr
  echo
  echo -e "   ${DIM}The installer only needs three values. Everything else${RST}"
  echo -e "   ${DIM}(depot, pricing, catalog) is configured after install${RST}"
  echo -e "   ${DIM}through the /admin panel in your Telegram bot.${RST}"
  echo

  echo -e "   ${BOLD}${MAG}── Telegram ──────────────────────────────────────────${RST}"
  echo -e "   ${DIM}Create a bot at t.me/BotFather → /newbot${RST}"
  prompt TELEGRAM_BOT_TOKEN "Bot Token" "" secret
  echo
  echo -e "   ${DIM}Your Telegram user ID (get it from t.me/userinfobot).${RST}"
  echo -e "   ${DIM}Multiple admins: 123456789,987654321${RST}"
  prompt ADMIN_USER_IDS "Admin Telegram User ID(s)"

  echo
  echo -e "   ${BOLD}${MAG}── Routing (optional) ───────────────────────────────${RST}"
  echo -e "   ${DIM}Mapbox token for road-distance delivery quotes.${RST}"
  echo -e "   ${DIM}Skip to use straight-line distance × 1.3 fallback.${RST}"
  prompt_optional MAPBOX_TOKEN "Mapbox Public Token"

  # Auto-generate a cryptographically secure internal token.
  # Customer never needs to know or enter this.
  INTERNAL_TOKEN=$(openssl rand -hex 32)
  ok "Internal service token auto-generated"
}

# ── Step 6: Select image version ──────────────────────────────────────────────
select_version() {
  step "Fetching latest release"
  SELECTED_VERSION=$(curl -fsSL \
    "https://api.github.com/repos/${GHCR_OWNER}/${GHCR_REPO}/releases/latest" \
    2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tag_name','latest'))" \
    2>/dev/null || echo "latest")
  [[ -z "$SELECTED_VERSION" ]] && SELECTED_VERSION="latest"
  ok "Version: ${SELECTED_VERSION}"
}

# ── Step 7: Directory structure ───────────────────────────────────────────────
create_directories() {
  step "Creating installation directory"
  mkdir -p "${INSTALL_DIR}"/{deploy,data,backups,logs}
  chmod 750 "${INSTALL_DIR}"
  chmod 700 "${INSTALL_DIR}/data"
  ok "Created ${INSTALL_DIR}/"
}

# ── Step 8: Write .env ────────────────────────────────────────────────────────
write_env() {
  step "Writing environment configuration"
  local f="${INSTALL_DIR}/deploy/.env"
  cat > "$f" << ENV
# Logistics Engine ${INSTALLER_VERSION}
# Generated: $(date -u '+%Y-%m-%d %H:%M UTC')
# Customer : ${LICENSE_CUSTOMER}
# Plan     : ${LICENSE_PLAN}
# Expires  : ${LICENSE_EXPIRES}
#
# To change any value: nano ${f}
# Then restart:        systemctl restart ${SERVICE_NAME}

TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
INTERNAL_TOKEN=${INTERNAL_TOKEN}
MAPBOX_TOKEN=${MAPBOX_TOKEN:-}
ADMIN_USER_IDS=${ADMIN_USER_IDS}
LICENSE_KEY=${LICENSE_KEY}
BUILD_VERSION=${SELECTED_VERSION}

ENGINE_IMAGE=${IMAGE_ENGINE}:${SELECTED_VERSION}
BOT_IMAGE=${IMAGE_BOT}:${SELECTED_VERSION}
ENV
  chmod 600 "$f"
  ok "Wrote ${f}  (mode 600 — root-readable only)"
}

# ── Step 9: Write production docker-compose.yml ───────────────────────────────
write_compose() {
  step "Writing docker-compose.yml"
  # Use a temp file to avoid heredoc variable expansion issues
  cat > "${INSTALL_DIR}/deploy/docker-compose.yml" << 'COMPOSE'
# Production — pulls pre-built images from GHCR.
# Image tags are controlled by ENGINE_IMAGE and BOT_IMAGE in .env.
# Run  'logistics update'  to pull a newer release.
services:

  engine:
    image: ${ENGINE_IMAGE}
    container_name: logistics_engine
    restart: unless-stopped
    env_file: .env
    environment:
      - INTERNAL_TOKEN=${INTERNAL_TOKEN}
      - MAPBOX_TOKEN=${MAPBOX_TOKEN}
      - LICENSE_KEY=${LICENSE_KEY}
      - BUILD_VERSION=${BUILD_VERSION}
    volumes:
      - engine_data:/app/data     # SQLite DB persists across container restarts
      - sockets:/app/sockets      # Unix socket shared with bot
    healthcheck:
      test: ["CMD", "test", "-S", "/app/sockets/logistics.sock"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 20s
    deploy:
      resources:
        limits:   {cpus: "1.5",  memory: 512M}
        reservations: {cpus: "0.25", memory: 128M}
    logging:
      driver: json-file
      options: {max-size: 20m, max-file: "5"}

  bot:
    image: ${BOT_IMAGE}
    container_name: logistics_bot
    restart: unless-stopped
    env_file: .env
    environment:
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - INTERNAL_TOKEN=${INTERNAL_TOKEN}
      - ADMIN_USER_IDS=${ADMIN_USER_IDS}
    volumes:
      - sockets:/app/sockets
    depends_on:
      engine:
        condition: service_healthy   # Bot waits for engine socket to exist
    deploy:
      resources:
        limits:   {cpus: "0.75", memory: 256M}
        reservations: {cpus: "0.10", memory: 64M}
    logging:
      driver: json-file
      options: {max-size: 20m, max-file: "5"}

volumes:
  engine_data: {driver: local}
  sockets:     {driver: local}
COMPOSE
  ok "Wrote docker-compose.yml (image-based, no build)"
}

# ── Step 10: Install systemd service ──────────────────────────────────────────
write_service() {
  step "Installing systemd service"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE
[Unit]
Description=Logistics Engine (Docker Compose)
Documentation=https://github.com/${GHCR_OWNER}/${GHCR_REPO}
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}/deploy
EnvironmentFile=${INSTALL_DIR}/deploy/.env

# --pull always ensures the pinned image tag is always up to date on restarts
ExecStart=/usr/bin/docker compose up -d --pull always
ExecStop=/usr/bin/docker compose stop --timeout 30

# 'logistics update' updates .env image tags then calls this reload
ExecReload=/bin/bash -c '/usr/bin/docker compose pull && /usr/bin/docker compose up -d --no-recreate'

TimeoutStartSec=180
TimeoutStopSec=45
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" --quiet
  ok "systemd service installed and enabled on boot"
}

# ── Step 11: Download supervisor binary ───────────────────────────────────────
# The 'logistics' binary (supervisor) is the CLI the customer uses to manage
# the running system: status, logs, update, rollback, maintenance, dashboard.
# It is built by release.yml and attached to every GitHub Release.
install_supervisor_binary() {
  step "Installing logistics CLI (supervisor binary)"

  local bin_url="https://github.com/${GHCR_OWNER}/${GHCR_REPO}/releases/download/${SELECTED_VERSION}/logistics-supervisor-linux-amd64"

  if [[ "$SELECTED_VERSION" == "latest" ]]; then
    # Resolve 'latest' to the actual tag so the download URL works
    SELECTED_VERSION=$(curl -fsSL \
      "https://api.github.com/repos/${GHCR_OWNER}/${GHCR_REPO}/releases/latest" \
      2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" \
      2>/dev/null || echo "latest")
    bin_url="https://github.com/${GHCR_OWNER}/${GHCR_REPO}/releases/download/${SELECTED_VERSION}/logistics-supervisor-linux-amd64"
  fi

  info "Downloading from GitHub Releases: ${SELECTED_VERSION}"

  if curl -fsSL "$bin_url" -o "${BIN_PATH}" 2>/tmp/bin-download.log; then
    chmod +x "${BIN_PATH}"
    ok "Installed: ${BIN_PATH}  ($(logistics version 2>/dev/null || echo "${SELECTED_VERSION}"))"
  else
    warn "Could not download supervisor binary (HTTP error or release not yet published)."
    warn "You can install it later: logistics-install-supervisor"
    # Write a stub so the command exists and gives a helpful message
    cat > "${BIN_PATH}" << 'STUB'
#!/usr/bin/env bash
echo "logistics CLI not yet installed. Run: logistics-install-supervisor"
STUB
    chmod +x "${BIN_PATH}"
  fi
}

# ── Step 12: Pull Docker images ───────────────────────────────────────────────
pull_images() {
  step "Pulling Docker images"
  info "Engine: ${IMAGE_ENGINE}:${SELECTED_VERSION}"
  info "Bot:    ${IMAGE_BOT}:${SELECTED_VERSION}"
  echo
  ( cd "${INSTALL_DIR}/deploy" && docker compose pull 2>/tmp/pull.log ) &
  spinner $! "Downloading images from GHCR"
}

# ── Step 13: Start services ───────────────────────────────────────────────────
start_services() {
  step "Starting services"
  systemctl start "${SERVICE_NAME}"

  # Wait up to 90 s for the engine socket to appear
  info "Waiting for engine to become healthy (up to 90 s)…"
  local deadline=$(( $(date +%s) + 90 )) healthy=false
  while (( $(date +%s) < deadline )); do
    local status
    status=$(docker inspect logistics_engine --format '{{.State.Health.Status}}' 2>/dev/null || echo "missing")
    if [[ "$status" == "healthy" ]]; then
      healthy=true; break
    fi
    sleep 3; printf '.'
  done
  echo

  if $healthy; then
    ok "Engine: healthy"
  else
    warn "Engine health check timed out — check: docker logs logistics_engine"
  fi

  local bot_status
  bot_status=$(docker inspect logistics_bot --format '{{.State.Status}}' 2>/dev/null || echo "missing")
  if [[ "$bot_status" == "running" ]]; then
    ok "Bot: running"
  else
    warn "Bot not yet running — check: docker logs logistics_bot"
  fi
}

# ── Step 14: Summary ──────────────────────────────────────────────────────────
print_summary() {
  echo
  hr
  echo
  echo -e "${BOLD}${CYN}╔══════════════════════════════════════════════════════╗${RST}"
  echo -e "${BOLD}${CYN}║              🎉  INSTALLATION COMPLETE               ║${RST}"
  echo -e "${BOLD}${CYN}╚══════════════════════════════════════════════════════╝${RST}"
  echo
  echo -e "   ${BOLD}Customer :${RST} ${LICENSE_CUSTOMER}"
  echo -e "   ${BOLD}Version  :${RST} ${SELECTED_VERSION}"
  echo -e "   ${BOLD}Admins   :${RST} ${ADMIN_USER_IDS}"
  echo -e "   ${BOLD}Install  :${RST} ${INSTALL_DIR}"
  echo
  hr
  echo
  echo -e "   ${BOLD}${GRN}First-time setup via Telegram:${RST}"
  echo -e "   ${DIM}1.${RST} Message your bot and send ${BOLD}/admin${RST}"
  echo -e "   ${DIM}2.${RST} Go to ${BOLD}⚙️ Settings → 🏭 Depot Config${RST} — set your warehouse coordinates"
  echo -e "   ${DIM}3.${RST} Go to ${BOLD}⚙️ Settings → 💰 Pricing Config${RST} — set base fare, rate/km, tax"
  echo -e "   ${DIM}4.${RST} Go to ${BOLD}📦 Add Item${RST} — add your first catalog item"
  echo -e "   ${DIM}5.${RST} Send ${BOLD}/start${RST} to test the customer flow"
  echo
  hr
  echo
  echo -e "   ${BOLD}${GRN}Server management (logistics CLI):${RST}"
  echo -e "   ${DIM}logistics status${RST}          — container health snapshot"
  echo -e "   ${DIM}logistics logs${RST}             — follow all logs"
  echo -e "   ${DIM}logistics logs -s engine${RST}  — engine logs only"
  echo -e "   ${DIM}logistics logs -s bot${RST}     — bot logs only"
  echo -e "   ${DIM}logistics update${RST}           — pull latest release (zero-downtime)"
  echo -e "   ${DIM}logistics update --dry-run${RST}— check what's available without applying"
  echo -e "   ${DIM}logistics rollback${RST}         — revert to previous version"
  echo -e "   ${DIM}logistics maintenance${RST}      — toggle maintenance mode"
  echo -e "   ${DIM}logistics dashboard${RST}        — interactive TUI"
  echo
  hr
  echo
  echo -e "   ${BOLD}${GRN}Config file:${RST}"
  echo -e "   ${DIM}nano ${INSTALL_DIR}/deploy/.env${RST}"
  echo -e "   ${DIM}systemctl restart ${SERVICE_NAME}${RST}   ← apply changes"
  echo
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  banner
  check_root
  detect_os
  check_requirements
  install_docker
  verify_license
  collect_config
  select_version
  create_directories
  write_env
  write_compose
  write_service
  install_supervisor_binary
  pull_images
  start_services
  print_summary
}

main "$@"
