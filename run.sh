#!/usr/bin/env bash
# =============================================================================
# run.sh  —  Pull latest code and start both bot services locally (macOS / EC2)
#
# Usage:
#   chmod +x run.sh
#   ./run.sh          # pull + start both services
#   ./run.sh --stop   # kill both services
#   ./run.sh --logs   # tail live logs from both services
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
UVICORN="$VENV_DIR/bin/uvicorn"
PID_AGENT="$SCRIPT_DIR/.pid_agent"
PID_WEBHOOK="$SCRIPT_DIR/.pid_webhook"
LOG_AGENT="$SCRIPT_DIR/.log_agent"
LOG_WEBHOOK="$SCRIPT_DIR/.log_webhook"
PORT_AGENT=5000
PORT_WEBHOOK=8000
BRANCH="bussiness_card_management"

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
warn()  { echo "[WARN]  $*" >&2; }

# ── Stop helpers ──────────────────────────────────────────────────────────────
kill_pid_file() {
    local pidfile="$1" label="$2"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" && ok "Stopped $label (PID $pid)"
        else
            warn "$label was not running (stale PID $pid)"
        fi
        rm -f "$pidfile"
    fi
}

stop_all() {
    info "Stopping services…"
    kill_pid_file "$PID_AGENT"   "Agent (ec2_endpoints)"
    kill_pid_file "$PID_WEBHOOK" "Webhook (webhook_main)"
    # Belt-and-suspenders: free the ports
    lsof -ti:"$PORT_AGENT"   | xargs kill -9 2>/dev/null || true
    lsof -ti:"$PORT_WEBHOOK" | xargs kill -9 2>/dev/null || true
    ok "All services stopped."
}

# ── --stop mode ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
    stop_all
    exit 0
fi

# ── --logs mode ───────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--logs" ]]; then
    info "Tailing logs (Ctrl-C to exit)…"
    tail -f "$LOG_AGENT" "$LOG_WEBHOOK"
    exit 0
fi

# ── 1. Pull latest code ───────────────────────────────────────────────────────
info "Pulling latest code from origin/$BRANCH…"
cd "$SCRIPT_DIR"
git fetch origin
git pull origin "$BRANCH" --ff-only || {
    warn "Fast-forward pull failed. Run 'git status' to check for local changes."
}
ok "Code up to date."

# ── 2. Ensure venv exists ─────────────────────────────────────────────────────
if [[ ! -x "$PYTHON" ]]; then
    info "Creating virtual environment…"
    python3 -m venv "$VENV_DIR"
fi

# ── 3. Sync dependencies ──────────────────────────────────────────────────────
info "Syncing Python dependencies…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
ok "Dependencies ready."

# ── 4. Check .env ─────────────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    warn ".env file not found! Services may fail to start. Create it from your env variables."
fi

# ── 5. Stop any old instances ────────────────────────────────────────────────
stop_all 2>/dev/null || true

# ── 6. Start Agent (ec2_endpoints) on port 5000 ──────────────────────────────
info "Starting Agent service on port $PORT_AGENT…"
"$UVICORN" ec2_endpoints:app \
    --host 0.0.0.0 \
    --port "$PORT_AGENT" \
    --workers 1 \
    --log-level info \
    > "$LOG_AGENT" 2>&1 &
echo $! > "$PID_AGENT"
ok "Agent started (PID $(cat "$PID_AGENT")). Log: $LOG_AGENT"

# ── 7. Brief pause so Agent is ready before Webhook starts ───────────────────
sleep 2

# ── 8. Start Webhook (webhook_main) on port 8000 ─────────────────────────────
info "Starting Webhook service on port $PORT_WEBHOOK…"
"$UVICORN" webhook_main:app \
    --host 0.0.0.0 \
    --port "$PORT_WEBHOOK" \
    --workers 1 \
    --log-level info \
    > "$LOG_WEBHOOK" 2>&1 &
echo $! > "$PID_WEBHOOK"
ok "Webhook started (PID $(cat "$PID_WEBHOOK")). Log: $LOG_WEBHOOK"

# ── 9. Health check ───────────────────────────────────────────────────────────
sleep 3
info "Checking service health…"
AGENT_UP=false
WEBHOOK_UP=false

if kill -0 "$(cat "$PID_AGENT")" 2>/dev/null; then
    AGENT_UP=true
    ok "Agent  is running on http://0.0.0.0:$PORT_AGENT"
else
    warn "Agent failed to start. Check log: $LOG_AGENT"
    cat "$LOG_AGENT" | tail -20
fi

if kill -0 "$(cat "$PID_WEBHOOK")" 2>/dev/null; then
    WEBHOOK_UP=true
    ok "Webhook is running on http://0.0.0.0:$PORT_WEBHOOK"
else
    warn "Webhook failed to start. Check log: $LOG_WEBHOOK"
    cat "$LOG_WEBHOOK" | tail -20
fi

echo ""
echo "========================================="
echo " Agent   : http://localhost:$PORT_AGENT  (PID $(cat "$PID_AGENT" 2>/dev/null || echo '?'))"
echo " Webhook : http://localhost:$PORT_WEBHOOK (PID $(cat "$PID_WEBHOOK" 2>/dev/null || echo '?'))"
echo ""
echo " Live logs:  ./run.sh --logs"
echo " Stop all:   ./run.sh --stop"
echo "========================================="

if [[ "$AGENT_UP" == "false" || "$WEBHOOK_UP" == "false" ]]; then
    exit 1
fi
