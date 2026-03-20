#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  deploy.sh — One-command deployment for tradebot + dashboard
#
#  Usage:
#    ./deploy.sh          # First deploy or redeploy
#    ./deploy.sh --down   # Stop and remove all containers
#    ./deploy.sh --logs   # Tail live logs from the bot
#
#  Requirements: Docker + Docker Compose (v2)
#  The .env file must exist before running.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[tradebot]${NC} $*"; }
success() { echo -e "${GREEN}[tradebot]${NC} $*"; }
warn()    { echo -e "${YELLOW}[tradebot]${NC} $*"; }
error()   { echo -e "${RED}[tradebot]${NC} $*" >&2; exit 1; }

# ── Flags ─────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--down" ]]; then
    info "Stopping all tradebot containers..."
    docker compose down
    success "Stopped."
    exit 0
fi

if [[ "${1:-}" == "--logs" ]]; then
    docker compose logs -f bot
    exit 0
fi

# ── Pre-flight checks ─────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  TRADEBOT DEPLOY${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

command -v docker &>/dev/null  || error "Docker not found. Install from https://docs.docker.com/get-docker/"
docker compose version &>/dev/null || error "Docker Compose v2 not found."

[[ -f ".env" ]] || error ".env file not found. Copy .env.example → .env and fill in your keys."

# ── Load .env ─────────────────────────────────────────────────────────────────
set -a; source .env; set +a

# ── Validate required env vars ────────────────────────────────────────────────
REQUIRED_VARS=(ALPACA_API_KEY ALPACA_API_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID DASHBOARD_USER DASHBOARD_PASSWORD)
for var in "${REQUIRED_VARS[@]}"; do
    [[ -n "${!var:-}" ]] || error "Missing required variable in .env: $var"
done

info "Environment validated ✓"

# ── Generate nginx .htpasswd from .env credentials ────────────────────────────
mkdir -p nginx

if command -v htpasswd &>/dev/null; then
    htpasswd -bc nginx/.htpasswd "$DASHBOARD_USER" "$DASHBOARD_PASSWORD"
    info "htpasswd generated via htpasswd ✓"
elif command -v python3 &>/dev/null; then
    python3 - <<PYEOF
import crypt, os
user = os.environ['DASHBOARD_USER']
pwd  = os.environ['DASHBOARD_PASSWORD']
hashed = crypt.crypt(pwd, crypt.mksalt(crypt.METHOD_SHA512))
with open('nginx/.htpasswd', 'w') as f:
    f.write(f"{user}:{hashed}\n")
print("htpasswd generated via python3 ✓")
PYEOF
else
    # Docker fallback — use apache2-utils inside a throwaway container
    docker run --rm \
        -e DASHBOARD_USER="$DASHBOARD_USER" \
        -e DASHBOARD_PASSWORD="$DASHBOARD_PASSWORD" \
        -v "$(pwd)/nginx:/out" \
        httpd:alpine \
        sh -c 'htpasswd -bc /out/.htpasswd "$DASHBOARD_USER" "$DASHBOARD_PASSWORD"'
    info "htpasswd generated via Docker fallback ✓"
fi

# ── Ensure logs dir exists (mounted as volume) ────────────────────────────────
mkdir -p logs
info "Logs directory ready ✓"

# ── Build & start ─────────────────────────────────────────────────────────────
info "Building Docker images (first build downloads FinBERT ~440MB, be patient)..."
docker compose build --pull

info "Starting containers..."
docker compose up -d

# ── Health check ──────────────────────────────────────────────────────────────
info "Waiting for dashboard to respond..."
for i in {1..20}; do
    if curl -sf http://localhost/healthz &>/dev/null; then
        break
    fi
    sleep 2
done

if curl -sf http://localhost/healthz &>/dev/null; then
    success "Dashboard is up ✓"
else
    warn "Dashboard didn't respond in time — it may still be starting up (FinBERT load takes ~30s)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  DEPLOYED ✓${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  Dashboard:  ${CYAN}http://$(curl -sf ifconfig.me 2>/dev/null || echo 'YOUR_SERVER_IP')${NC}"
echo -e "  Login:      ${YELLOW}${DASHBOARD_USER}${NC} / (your password)"
echo ""
echo -e "  Bot logs:   ${CYAN}./deploy.sh --logs${NC}"
echo -e "  Stop all:   ${CYAN}./deploy.sh --down${NC}"
echo -e "  Rebuild:    ${CYAN}./deploy.sh${NC}"
echo ""
