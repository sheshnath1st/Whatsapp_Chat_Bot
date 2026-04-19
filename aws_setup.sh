#!/usr/bin/env bash
# =============================================================================
# aws_setup.sh  —  Full EC2 instance bootstrap + clean restart for the
#                  WhatsApp Llama-4 bot (ec2_endpoints + webhook_main).
#
# Usage:
#   chmod +x aws_setup.sh
#   sudo ./aws_setup.sh            # first-time full setup
#   sudo ./aws_setup.sh --restart  # stop services, pull latest, restart
#   sudo ./aws_setup.sh --clean    # stop services and purge tmp/logs/cache
# =============================================================================
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
APP_DIR="${APP_DIR:-/home/ubuntu/whatsapp_llama_4_bot}"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
EC2_PORT_AGENT=5000
EC2_PORT_WEBHOOK=8000
LOG_DIR="/var/log/whatsapp_bot"
SWAP_SIZE_MB=2048   # 2 GB swap — critical for Whisper on t2/t3.micro
SERVICE_USER="${SERVICE_USER:-ubuntu}"

MODE="${1:-}"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*" >&2; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "Run with sudo / as root"
}

# ── Stop services ─────────────────────────────────────────────────────────────
stop_services() {
    info "Stopping bot services (if running)…"
    systemctl stop whatsapp-agent.service  2>/dev/null || true
    systemctl stop whatsapp-webhook.service 2>/dev/null || true
    # Kill any orphaned uvicorn processes on our ports
    fuser -k "${EC2_PORT_AGENT}/tcp"   2>/dev/null || true
    fuser -k "${EC2_PORT_WEBHOOK}/tcp" 2>/dev/null || true
    info "Services stopped."
}

# ── Clean temp / cache files ──────────────────────────────────────────────────
clean_temp() {
    info "Cleaning temporary files…"
    # Audio temp files written by handle_audio_message
    find /tmp -maxdepth 1 \( -name "*.ogg" -o -name "*.opus" -o -name "*.mp3" \
        -o -name "*.m4a" -o -name "*.wav" -o -name "*.webm" \
        -o -name "*.normalized.wav" \) -mmin +30 -delete 2>/dev/null || true
    # Python __pycache__
    find "$APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    # Whisper model cache (force re-download on next start; use only when upgrading)
    # Uncomment the next line to purge cached Whisper weights too:
    # rm -rf ~/.cache/whisper
    info "Temp clean done."
}

# ── Rotate logs ───────────────────────────────────────────────────────────────
rotate_logs() {
    info "Rotating log files…"
    mkdir -p "$LOG_DIR"
    for f in "$LOG_DIR"/*.log; do
        [[ -f "$f" ]] || continue
        mv "$f" "${f%.log}-$(date +%Y%m%d_%H%M%S).log"
    done
    # Purge rotated logs older than 7 days
    find "$LOG_DIR" -name "*.log" -mtime +7 -delete 2>/dev/null || true
    info "Log rotation done."
}

# ── --clean mode ──────────────────────────────────────────────────────────────
if [[ "$MODE" == "--clean" ]]; then
    require_root
    stop_services
    clean_temp
    rotate_logs
    info "Clean complete. Start services manually or re-run without --clean."
    exit 0
fi

# ── --restart mode ─────────────────────────────────────────────────────────────
if [[ "$MODE" == "--restart" ]]; then
    require_root
    stop_services
    clean_temp

    info "Pulling latest code…"
    cd "$APP_DIR"
    git pull --ff-only || warn "git pull skipped (no remote or detached HEAD)"

    info "Updating Python dependencies…"
    "$VENV_DIR/bin/pip" install --quiet --upgrade -r requirements.txt

    rotate_logs
    systemctl start whatsapp-agent.service
    systemctl start whatsapp-webhook.service
    info "Services restarted."
    systemctl status whatsapp-agent.service --no-pager || true
    systemctl status whatsapp-webhook.service --no-pager || true
    exit 0
fi

# ── Full first-time setup ─────────────────────────────────────────────────────
require_root
info "=== Full AWS EC2 setup for WhatsApp Llama-4 Bot ==="

# 1. System packages
info "Updating apt and installing system packages…"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    ffmpeg \
    tesseract-ocr \
    libtesseract-dev \
    git \
    fuser \
    logrotate

# 2. Swap space (vital for Whisper on low-RAM instances)
if ! swapon --show | grep -q "[0-9]"; then
    info "Creating ${SWAP_SIZE_MB}MB swap file at /swapfile…"
    if [[ ! -f /swapfile ]]; then
        fallocate -l "${SWAP_SIZE_MB}M" /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Reduce swappiness so RAM is preferred over swap
    sysctl -w vm.swappiness=10
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
    info "Swap enabled."
else
    info "Swap already active — skipping."
fi

# 3. Python virtual environment
info "Setting up Python virtual environment at $VENV_DIR…"
[[ -d "$APP_DIR" ]] || die "App directory $APP_DIR not found. Clone the repo first."
cd "$APP_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip setuptools wheel

# 4. Python dependencies
info "Installing Python requirements…"
# Reduce memory during install with --no-cache-dir
"$VENV_DIR/bin/pip" install --quiet --no-cache-dir -r requirements.txt

# 5. .env file sanity check
if [[ ! -f "$APP_DIR/.env" ]]; then
    warn ".env file missing! Copy .env.example or create it before starting services."
fi

# 6. Log directory
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

# 7. systemd service — ec2_endpoints (agent / LLM layer)
info "Writing systemd service: whatsapp-agent.service…"
cat > /etc/systemd/system/whatsapp-agent.service <<EOF
[Unit]
Description=WhatsApp Bot — EC2 Agent (LLM / business-card endpoint)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment="MALLOC_ARENA_MAX=2"
ExecStart=$VENV_DIR/bin/uvicorn ec2_endpoints:app \
    --host 0.0.0.0 \
    --port $EC2_PORT_AGENT \
    --workers 1 \
    --log-level info
Restart=on-failure
RestartSec=5s
StandardOutput=append:$LOG_DIR/agent.log
StandardError=append:$LOG_DIR/agent.log

[Install]
WantedBy=multi-user.target
EOF

# 8. systemd service — webhook_main (Meta webhook receiver)
info "Writing systemd service: whatsapp-webhook.service…"
cat > /etc/systemd/system/whatsapp-webhook.service <<EOF
[Unit]
Description=WhatsApp Bot — Webhook Receiver
After=network.target whatsapp-agent.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment="MALLOC_ARENA_MAX=2"
ExecStart=$VENV_DIR/bin/uvicorn webhook_main:app \
    --host 0.0.0.0 \
    --port $EC2_PORT_WEBHOOK \
    --workers 1 \
    --log-level info
Restart=on-failure
RestartSec=5s
StandardOutput=append:$LOG_DIR/webhook.log
StandardError=append:$LOG_DIR/webhook.log

[Install]
WantedBy=multi-user.target
EOF

# 9. logrotate config
info "Configuring logrotate…"
cat > /etc/logrotate.d/whatsapp-bot <<EOF
$LOG_DIR/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

# 10. Enable + start services
info "Enabling and starting services…"
systemctl daemon-reload
systemctl enable whatsapp-agent.service
systemctl enable whatsapp-webhook.service
systemctl start whatsapp-agent.service
systemctl start whatsapp-webhook.service

# 11. Status check
sleep 2
info "=== Service Status ==="
systemctl status whatsapp-agent.service  --no-pager || true
systemctl status whatsapp-webhook.service --no-pager || true

info ""
info "=== Setup complete ==="
info "Agent  log : $LOG_DIR/agent.log"
info "Webhook log: $LOG_DIR/webhook.log"
info ""
info "Manage with:"
info "  sudo systemctl {start|stop|restart|status} whatsapp-agent.service"
info "  sudo systemctl {start|stop|restart|status} whatsapp-webhook.service"
info "  sudo ./aws_setup.sh --restart   # pull latest + restart"
info "  sudo ./aws_setup.sh --clean     # purge tmp files + logs"
