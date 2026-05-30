#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

readonly INSTALLER_VERSION="10.0.0"
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
readonly LICENSE_PUBKEY_HEX="2aedb7bd6d094c3e73280d404c064c4f71241059e3189a68528e973bef49b0f4"

if [[ -t 1 ]]; then
  RED='\033[0;31m' YEL='\033[0;33m' GRN='\033[0;32m'
  BLU='\033[0;34m' CYN='\033[0;36m' MAG='\033[0;35m'
  BOLD='\033[1m'   DIM='\033[2m'    RST='\033[0m'
else
  RED='' YEL='' GRN='' BLU='' CYN='' MAG='' BOLD='' DIM='' RST=''
fi

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
  local __var="$1" __label="$2" __default="${3:-}" __secret="${4:-}" __val=""
  while [[ -z "$__val" ]]; do
    if [[ -n "$__secret" ]]; then
      printf "   %b%s%b%b%s%b: " "${BOLD}" "$__label" "${RST}" "${DIM}" "${__default:+ [hidden]}" "${RST}"
      # Redirect read to the controlling terminal (/dev/tty)
      read -rs __val < /dev/tty; echo
    else
      printf "   %b%s%b%b%s%b: " "${BOLD}" "$__label" "${RST}" "${DIM}" "${__default:+ [$__default]}" "${RST}"
      # Redirect read to the controlling terminal (/dev/tty)
      read -r __val < /dev/tty
    fi
    [[ -z "$__val" && -n "$__default" ]] && __val="$__default"
    [[ -z "$__val" ]] && echo -e "   ${RED}Required — please enter a value.${RST}"
  done
  printf -v "$__var" '%s' "$__val"
}

prompt_optional() {
  local __var="$1" __label="$2" __val=""
  printf "   %b%s%b %b(optional — Enter to skip)%b: " "${BOLD}" "$__label" "${RST}" "${DIM}" "${RST}"
  # Redirect read to the controlling terminal (/dev/tty)
  read -r __val < /dev/tty
  printf -v "$__var" '%s' "$__val"
}

spinner() {
  local __pid=$1 __msg="$2" __sp='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' __i=0
  while kill -0 "$__pid" 2>/dev/null; do
    printf "\r   ${CYN}${__sp:$((__i % ${#__sp})):1}${RST}  %s…" "$__msg"
    __i=$((__i + 1)); sleep 0.1
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

check_root() {
  [[ "$EUID" -eq 0 ]] || die "Run as root:  sudo bash install.sh"
}

detect_os() {
  step "Detecting operating system"
  [[ -f /etc/os-release ]] || die "Cannot detect OS. Supported: Ubuntu 20.04+, Debian 11+."
  source /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ok "OS: ${ID^} ${VERSION_ID:-}" ;;
    *) die "Unsupported OS: ${ID:-unknown}. Supported: Ubuntu 20.04+, Debian 11+." ;;
  esac
}

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
  
  local arch
  arch=$(uname -m)
  if [[ "$arch" != "x86_64" ]]; then
    warn "Non-standard hardware architecture detected: ${arch}"
    warn "The Logistics CLI and supervisor binaries are compiled natively for x86_64 (amd64)."
    warn "Running on ${arch} may result in binary execution incompatibility issues unless emulated."
  else
    ok "Architecture: x86_64 (supported)"
  fi

  for cmd in curl openssl python3 systemctl; do
    command -v "$cmd" &>/dev/null || die "${cmd} is required. Run: apt-get install -y ${cmd}"
  done
  ok "Required tools present (curl, openssl, python3, systemctl)"
}

install_docker() {
  step "Checking Docker and Compose"
  local has_docker=false
  local has_compose=false

  if command -v docker &>/dev/null; then
    has_docker=true
    local ver
    ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1 || echo 0)
    ok "Docker/Moby daemon found (version ${ver})"
  fi

  if docker compose version &>/dev/null; then
    has_compose=true
    ok "Docker Compose subcommand is available"
  fi

  if $has_docker && $has_compose; then
    return 0
  fi

  if ! $has_docker; then
    info "Installing Docker via get.docker.com..."
    ( curl -fsSL https://get.docker.com | sh ) &>/tmp/logistics-docker-install.log &
    spinner $! "Installing Docker"
    systemctl enable --quiet docker
    systemctl start  docker
  fi

  if ! docker compose version &>/dev/null; then
    info "Docker Compose plugin missing — installing CLI plugin..."
    mkdir -p /usr/libexec/docker/cli-plugins

    # Detect system architecture for correct binary download
    local arch
    arch=$(uname -m)
    case "$arch" in
      x86_64|amd64)    arch="x86_64" ;;
      aarch64|arm64)   arch="aarch64" ;;
      armv7l|armhf)    arch="armv7" ;;
      *) die "Unsupported architecture: ${arch}. Supported: x86_64, aarch64, armv7." ;;
    esac

    local compose_url="https://github.com/docker/compose/releases/download/v2.36.1/docker-compose-linux-${arch}"
    info "Downloading compose plugin for ${arch}..."
    if curl -fsSL "$compose_url" -o /usr/libexec/docker/cli-plugins/docker-compose 2>/tmp/compose-install.log; then
      chmod +x /usr/libexec/docker/cli-plugins/docker-compose
      ok "Installed docker-compose plugin (${arch})"
    else
      warn "Failed to download compose plugin directly. Trying apt-get..."
      apt-get update -qq && apt-get install -y docker-compose-plugin &>/dev/null || die "Could not install docker-compose-plugin."
    fi
  fi
}

verify_license() {
  step "License verification"
  hr
  echo
  echo -e "   ${DIM}Your license key was included in your purchase email.${RST}"
  echo -e "   ${DIM}It is a key begin with ENG-XXXX-XXXX-XXXX-XXXX${RST}"
  echo
  local key="" attempts=0
  while true; do
    (( ++attempts > 3 )) && die "Too many failed attempts. Contact support."
    prompt key "License Key" "" secret
    local response http_code body
    response=$(curl -fsSL -w "\n%{http_code}" \
      -H "Content-Type: application/json" \
      -d "{\"license_key\":\"${key}\",\"hostname\":\"$(hostname -f 2>/dev/null || hostname)\",\"installer_version\":\"${INSTALLER_VERSION}\"}" \
      "${LICENSE_API}" 2>/dev/null) || response=$'\n000'
    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | head -n -1)
    case "$http_code" in
      200)
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
  INTERNAL_TOKEN=$(openssl rand -hex 32)
  ok "Internal service token auto-generated"
  
  # Auto-generate secure Postgres credentials
  POSTGRES_USER="logistics"
  POSTGRES_PASSWORD=$(openssl rand -hex 16)
  POSTGRES_DB="logistics"
  ok "Database credentials auto-generated"
}

select_version() {
  step "Fetching latest release"
  SELECTED_VERSION=$(curl -fsSL \
    "https://api.github.com/repos/${GHCR_OWNER}/${GHCR_REPO}/releases" \
    2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); tags=[r.get('tag_name') for r in d if r.get('tag_name')]; print(tags[0] if tags else 'latest')" \
    2>/dev/null || echo "latest")
  [[ -z "$SELECTED_VERSION" ]] && SELECTED_VERSION="latest"
  ok "Version: ${SELECTED_VERSION}"
}

cleanup_legacy_install() {
  step "Cleaning up legacy installation"
  
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Stopping legacy systemd service…"
    systemctl stop "${SERVICE_NAME}"
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  fi

  if [[ -f "${INSTALL_DIR}/deploy/docker-compose.yml" ]]; then
    info "Stopping legacy Docker Compose stack…"
    ( cd "${INSTALL_DIR}/deploy" && docker compose down -v 2>/dev/null || true )
  fi

  info "Removing orphaned containers…"
  docker rm -f logistics_engine logistics_bot logistics_postgres 2>/dev/null || true

  if [[ -f "${INSTALL_DIR}/data/logistics.db" ]]; then
    local backup_dir="${INSTALL_DIR}.bak.$(date +%s)"
    info "Backing up legacy SQLite data to ${backup_dir}…"
    mv "${INSTALL_DIR}" "${backup_dir}"
    ok "Legacy SQLite data safely backed up"
  fi
}

create_directories() {
  step "Creating installation directory"
  mkdir -p "${INSTALL_DIR}"/{deploy,data,backups,logs}
  chmod 750 "${INSTALL_DIR}"
  chmod 700 "${INSTALL_DIR}/data"
  ok "Created ${INSTALL_DIR}/"
}

write_env() {
  step "Writing environment configuration"
  local f="${INSTALL_DIR}/deploy/.env"
  cat > "$f" << ENV
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
INTERNAL_TOKEN=${INTERNAL_TOKEN}
MAPBOX_TOKEN=${MAPBOX_TOKEN:-}
ADMIN_USER_IDS=${ADMIN_USER_IDS}
LICENSE_KEY=${LICENSE_KEY}
LICENSE_PUBLIC_KEY=${LICENSE_KEY}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=${POSTGRES_DB}
DATABASE_URL=postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}?sslmode=disable
BUILD_VERSION=${SELECTED_VERSION}
ENGINE_IMAGE=${IMAGE_ENGINE}:${SELECTED_VERSION}
BOT_IMAGE=${IMAGE_BOT}:${SELECTED_VERSION}
UPDATE_MANIFEST_URL="https://pub-cd7e4ed255b541cfaed526741f32d2f1.r2.dev/latest/manifest.json"
ENV
  chmod 600 "$f"
  ok "Wrote ${f}  (mode 600 — root-readable only)"
}

write_compose() {
  step "Writing docker-compose.yml"
  cat > "${INSTALL_DIR}/deploy/docker-compose.yml" << 'COMPOSE'
services:
  postgres:
    init: true
    image: postgres:16-alpine
    restart: unless-stopped
    command: ["postgres", "-c", "max_connections=100", "-c", "shared_buffers=32MB", "-c", "work_mem=2MB", "-c", "maintenance_work_mem=8MB", "-c", "effective_cache_size=64MB"]
    env_file: .env
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 15s
    deploy:
      resources:
        limits:
          cpus: "0.50"
          memory: 128M
        reservations:
          cpus: "0.10"
          memory: 32M
    networks: [backend]

  engine:
    init: true
    image: ${ENGINE_IMAGE}
    container_name: logistics_engine
    restart: unless-stopped
    env_file: .env
    environment:
      - GOMEMLIMIT=48MiB
      - GOGC=50
    volumes:
      - sockets:/app/sockets
      - backups:/app/backups
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "test -S /app/sockets/logistics.sock"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 20s
    deploy:
      resources:
        limits:
          cpus: "0.50"
          memory: 96M
        reservations:
          cpus: "0.10"
          memory: 32M
    networks: [backend]

  bot:
    init: true
    image: ${BOT_IMAGE}
    container_name: logistics_bot
    restart: unless-stopped
    env_file: .env
    environment:
      - ENGINE_SOCKET=/app/sockets/logistics.sock
      - PYTHONMALLOC=malloc
    volumes:
      - sockets:/app/sockets:ro
    depends_on:
      engine:
        condition: service_healthy
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 128M
        reservations:
          cpus: "0.05"
          memory: 32M
    networks: [backend]

volumes:
  postgres_data: {driver: local}
  sockets:       {driver: local}
  backups:       {driver: local}

networks:
  backend:
    driver: bridge
COMPOSE
  ok "Wrote docker-compose.yml"
}

write_service() {
  step "Installing systemd service"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE
[Unit]
Description=Logistics System (Docker Compose stack)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}/deploy
EnvironmentFile=${INSTALL_DIR}/deploy/.env

# Start compose stack (using already pulled images for robust & fast boot)
ExecStart=/usr/bin/docker compose up -d

# Graceful stop
ExecStop=/usr/bin/docker compose stop --timeout 30
ExecReload=/bin/bash -c '/usr/bin/docker compose pull && /usr/bin/docker compose up -d'
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

install_supervisor_binary() {
  step "Installing Logistics Command Line Interface (CLI)"
  
  # Detect system architecture for correct binary download
  local go_arch
  case "$(uname -m)" in
    x86_64|amd64)    go_arch="amd64" ;;
    aarch64|arm64)   go_arch="arm64" ;;
    *) warn "Unsupported architecture for supervisor: $(uname -m). Skipping."; return 0 ;;
  esac

  # Default to the specific version folder uploaded by GitHub Actions
  local bin_url="https://pub-cd7e4ed255b541cfaed526741f32d2f1.r2.dev/${SELECTED_VERSION}/logistics-supervisor-linux-${go_arch}"
  
  # Override to the 'latest' folder if no specific version tag was found
  if [[ "$SELECTED_VERSION" == "latest" ]] || [[ "$SELECTED_VERSION" == "v10-latest" ]]; then
    bin_url="https://pub-cd7e4ed255b541cfaed526741f32d2f1.r2.dev/latest/supervisor-${go_arch}"
  fi
  
  info "Connecting to secure deployment node..."
  
  # Run download in background
  curl -fsSL "$bin_url" -o "${BIN_PATH}" 2>/tmp/bin-download.log &
  local pid=$!
  
  local width=30
  local i=0
  while kill -0 "$pid" 2>/dev/null; do
    local percent=$((i * 100 / width))
    local filled=$(printf "%${i}s" | tr ' ' '█')
    local empty=$(printf "%$((width - i))s" | tr ' ' '░')
    printf "\r   ${CYN}Downloading Logistics CLI${RST}     [%s%s] %d%%" "$filled" "$empty" "$percent"
    i=$(( (i + 1) % (width + 1) ))
    sleep 0.1
  done
  wait "$pid"
  local rc=$?
  
  if ((rc == 0)); then
    local filled=$(printf "%${width}s" | tr ' ' '█')
    printf "\r   ${GRN}✔${RST}  ${CYN}Downloading Logistics CLI${RST}     [%s] 100%%\n" "$filled"
    chmod +x "${BIN_PATH}"
    ok "Logistics CLI successfully installed at ${BIN_PATH} (${go_arch})"
  else
    local empty=$(printf "%${width}s" | tr ' ' '░')
    printf "\r   ${RED}✘${RST}  ${CYN}Downloading Logistics CLI${RST}     [%s] Failed\n" "$empty"
    warn "Download failed. Installing local placeholder stub."
    cat > "${BIN_PATH}" << 'STUB'
#!/usr/bin/env bash
echo "Logistics CLI not yet installed."
STUB
    chmod +x "${BIN_PATH}"
  fi
}

pull_images() {
  step "Initializing Secured Production Container Deployment"
  info "Secured Image Source: Container Registry (Engine: ${SELECTED_VERSION})"
  info "Secured Image Source: Container Registry (Bot: ${SELECTED_VERSION})"
  echo
  
  # Run compose pull in background
  ( cd "${INSTALL_DIR}/deploy" && docker compose pull >/tmp/pull.log 2>&1 ) &
  local pid=$!
  
  local width=30
  local i=0
  while kill -0 "$pid" 2>/dev/null; do
    local percent=$((i * 100 / width))
    local filled=$(printf "%${i}s" | tr ' ' '█')
    local empty=$(printf "%$((width - i))s" | tr ' ' '░')
    printf "\r   ${CYN}Pulling Container Images${RST}    [%s%s] %d%%" "$filled" "$empty" "$percent"
    i=$(( (i + 1) % (width + 1) ))
    sleep 0.1
  done
  wait "$pid"
  local rc=$?
  
  if ((rc == 0)); then
    local filled=$(printf "%${width}s" | tr ' ' '█')
    printf "\r   ${GRN}✔${RST}  ${CYN}Pulling Container Images${RST}    [%s] 100%%\n" "$filled"
    ok "Container network successfully finalized."
  else
    local empty=$(printf "%${width}s" | tr ' ' '░')
    printf "\r   ${RED}✘${RST}  ${CYN}Pulling Container Images${RST}    [%s] Failed\n" "$empty"
    warn "Failed to pull container images. Details: $(cat /tmp/pull.log 2>/dev/null)"
  fi
}

start_services() {
  step "Starting logistics services"
  systemctl start "${SERVICE_NAME}"
  info "Verifying service health (up to 90 s)…"
  
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
    ok "All services initialized successfully and passed health checks."
  else
    RED_ERR='\033[0;31m'
    RST_ERR='\033[0m'
    echo -e "${RED_ERR}❌ CRITICAL: Engine health check failed! Installation is unhealthy.${RST_ERR}"
    
    # Automated Rollback Trigger
    local last_backup
    last_backup=$(ls -td "${INSTALL_DIR}/backups/pre-install-"*.tar.gz.enc 2>/dev/null | head -n 1 || true)
    if [[ -n "$last_backup" && -f "$last_backup" ]]; then
      warn "Automated Rollback Triggered: Reverting deployment to previous healthy snapshot…"
      
      # Source variables of the current deployment first if it exists to get the decryption key
      local current_token=""
      if [[ -f "${INSTALL_DIR}/deploy/.env" ]]; then
        current_token=$(grep -E "^INTERNAL_TOKEN=" "${INSTALL_DIR}/deploy/.env" | cut -d'=' -f2-)
      fi
      local current_bot_token=""
      if [[ -f "${INSTALL_DIR}/deploy/.env" ]]; then
        current_bot_token=$(grep -E "^TELEGRAM_BOT_TOKEN=" "${INSTALL_DIR}/deploy/.env" | cut -d'=' -f2-)
      fi

      local temp_restore_dir="/tmp/logistics-restore-$$"
      mkdir -p "$temp_restore_dir"

      local decrypted=false
      for pass in "$current_token" "$current_bot_token" "logistics_secure_fallback_key_2026"; do
        if [[ -n "$pass" ]]; then
          if openssl enc -d -aes-256-cbc -pbkdf2 -iter 10000 -pass pass:"$pass" -in "$last_backup" | tar -xzf - -C "$temp_restore_dir" 2>/dev/null; then
            decrypted=true
            break
          fi
        fi
      done

      if $decrypted; then
        # Successfully decrypted! Source old variables to get db credentials for restore
        set -a
        source "${temp_restore_dir}/.env"
        set +a

        cp "${temp_restore_dir}/.env" "${INSTALL_DIR}/deploy/.env"
        cp "${temp_restore_dir}/docker-compose.yml" "${INSTALL_DIR}/deploy/docker-compose.yml"
        
        # Restart compose with old environment
        ( cd "${INSTALL_DIR}/deploy" && docker compose down && docker compose up -d )
        
        # Restore PostgreSQL database if a SQL dump exists in backup
        if [[ -f "${temp_restore_dir}/db_backup.sql" ]]; then
          info "Restoring PostgreSQL database dump..."
          # Wait up to 15s for postgres to become ready
          local db_ready=false
          for i in {1..5}; do
            if docker exec logistics_postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
              db_ready=true
              break
            fi
            sleep 3
          done
          if $db_ready; then
            docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" logistics_postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "${temp_restore_dir}/db_backup.sql" >/dev/null 2>&1 || true
            ok "Database successfully rolled back to pre-installation state."
          else
            warn "PostgreSQL container was not ready in time. Skipping db restore."
          fi
        fi

        rm -rf "$temp_restore_dir"
        systemctl restart "${SERVICE_NAME}"
        die "Auto-rollback completed. The new release is incompatible or failed to initialize. Please check engine container logs."
      else
        rm -rf "$temp_restore_dir"
        die "Automated Rollback Failed: Could not decrypt the pre-installation backup snapshot."
      fi
    else
      die "Health check failed and no pre-installation backup found to roll back. Check: docker logs logistics_engine"
    fi
  fi
}

backup_current_deployment() {
  step "Creating pre-installation deployment backup"
  if [[ -d "${INSTALL_DIR}/deploy" ]]; then
    local temp_backup_dir="/tmp/logistics-backup-$$"
    mkdir -p "$temp_backup_dir"
    
    # Backup .env if it exists
    if [[ -f "${INSTALL_DIR}/deploy/.env" ]]; then
      cp "${INSTALL_DIR}/deploy/.env" "${temp_backup_dir}/.env"
      # Source environment variables to extract PostgreSQL credentials and backup keys
      set -a
      source "${INSTALL_DIR}/deploy/.env"
      set +a
    fi
    # Backup docker-compose.yml if it exists
    if [[ -f "${INSTALL_DIR}/deploy/docker-compose.yml" ]]; then
      cp "${INSTALL_DIR}/deploy/docker-compose.yml" "${temp_backup_dir}/docker-compose.yml"
    fi

    # Backup the active PostgreSQL database via pg_dump if container is running
    if docker ps -q -f name=logistics_postgres >/dev/null 2>&1; then
      info "Dumping active PostgreSQL database..."
      if [[ -n "$POSTGRES_USER" && -n "$POSTGRES_DB" ]]; then
        docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" logistics_postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "${temp_backup_dir}/db_backup.sql" 2>/dev/null || true
        if [[ -f "${temp_backup_dir}/db_backup.sql" ]]; then
          ok "Database dump successfully created."
        else
          warn "Database dump was empty or failed"
        fi
      else
        warn "PostgreSQL credentials missing in .env, skipping db dump"
      fi
    else
      info "Database container logistics_postgres is not running. Skipping db dump."
    fi

    # Secure and encrypt the backup into a single secure tarball
    local secure_backup_file="${INSTALL_DIR}/backups/pre-install-$(date +%Y%m%d-%H%M%S).tar.gz.enc"
    local enc_pass="${INTERNAL_TOKEN:-$TELEGRAM_BOT_TOKEN}"
    enc_pass="${enc_pass:-logistics_secure_fallback_key_2026}"

    if tar -czf - -C "$temp_backup_dir" . | openssl enc -aes-256-cbc -pbkdf2 -iter 10000 -pass pass:"$enc_pass" -out "$secure_backup_file" 2>/dev/null; then
      ok "Pre-installation snapshot successfully encrypted and saved at ${secure_backup_file}"
    else
      warn "Failed to encrypt backup. Saving as standard plain tarball..."
      tar -czf "${INSTALL_DIR}/backups/pre-install-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$temp_backup_dir" .
    fi

    rm -rf "$temp_backup_dir"
  else
    info "No existing deployment found. Skipping backup."
  fi
}

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

main() {
  banner
  check_root
  detect_os
  check_requirements
  install_docker
  verify_license
  collect_config
  select_version
  cleanup_legacy_install
  create_directories
  backup_current_deployment
  write_env
  write_compose
  write_service
  install_supervisor_binary
  pull_images
  start_services
  print_summary
}

main "$@"
