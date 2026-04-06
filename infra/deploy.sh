#!/bin/bash
# =============================================================================
# deploy.sh — Deploy the trading bot to the EC2 instance with RDS
#
# Reads connection info from the state file created by setup-aws.sh,
# clones the repo, creates .env with RDS credentials, runs migrations,
# and starts the services using docker-compose.prod.yml (no local postgres).
#
# Usage:
#   ./infra/deploy.sh [--repo <GIT_REPO_URL>] [--branch main] [--env-file .env]
#
# Prerequisites:
#   - setup-aws.sh has been run (state file exists)
#   - A .env file exists locally with all required credentials
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Colors for output
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
REPO_URL="https://github.com/fededz/tb.git"
BRANCH="main"
ENV_FILE=".env"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_FILE="${SCRIPT_DIR}/.aws-state"
REMOTE_DIR="/opt/trading-bot"

# -----------------------------------------------------------------------------
# Parse arguments
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            REPO_URL="$2"
            shift 2
            ;;
        --branch)
            BRANCH="$2"
            shift 2
            ;;
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--repo <GIT_REPO_URL>] [--branch main] [--env-file .env]"
            echo ""
            echo "Defaults:"
            echo "  --repo     https://github.com/fededz/tb.git"
            echo "  --branch   main"
            echo "  --env-file .env"
            exit 0
            ;;
        *)
            err "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# -----------------------------------------------------------------------------
# Load state from setup-aws.sh
# -----------------------------------------------------------------------------
if [[ ! -f "$STATE_FILE" ]]; then
    err "State file not found at ${STATE_FILE}"
    echo "  Run setup-aws.sh first to provision the infrastructure."
    exit 1
fi

# shellcheck source=/dev/null
source "$STATE_FILE"

# Validate required state variables
for var in PUBLIC_IP KEY_NAME RDS_ENDPOINT RDS_PORT RDS_DB_NAME RDS_MASTER_USER RDS_MASTER_PASSWORD; do
    if [[ -z "${!var:-}" ]]; then
        err "Missing ${var} in state file. Re-run setup-aws.sh."
        exit 1
    fi
done

# Resolve .env file path (relative to project root if not absolute)
if [[ ! "$ENV_FILE" = /* ]]; then
    ENV_FILE="${PROJECT_DIR}/${ENV_FILE}"
fi

if [[ ! -f "$ENV_FILE" ]]; then
    err ".env file not found at ${ENV_FILE}"
    echo "  Create a .env file with the required credentials before deploying."
    exit 1
fi

# Determine SSH key path
if [[ "$KEY_AUTO_CREATED" == "true" ]]; then
    SSH_KEY="${SCRIPT_DIR}/${KEY_NAME}.pem"
else
    SSH_KEY="$HOME/.ssh/${KEY_NAME}.pem"
fi

if [[ ! -f "$SSH_KEY" ]]; then
    err "SSH key not found at ${SSH_KEY}"
    exit 1
fi

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
SSH_CMD="ssh ${SSH_OPTS} -i ${SSH_KEY} ec2-user@${PUBLIC_IP}"
SCP_CMD="scp ${SSH_OPTS} -i ${SSH_KEY}"

echo ""
echo "========================================="
echo "  Trading Bot — Deploy"
echo "========================================="
echo ""
info "Target:       ec2-user@${PUBLIC_IP}"
info "Repo:         ${REPO_URL} (branch: ${BRANCH})"
info "Env file:     ${ENV_FILE}"
info "RDS endpoint: ${RDS_ENDPOINT}:${RDS_PORT}"
echo ""

# =============================================================================
# Step 1: Wait for SSH
# =============================================================================
info "[1/7] Waiting for SSH to become available..."
MAX_RETRIES=30
RETRY=0
until $SSH_CMD "echo 'SSH ready'" &>/dev/null; do
    RETRY=$((RETRY + 1))
    if [[ $RETRY -ge $MAX_RETRIES ]]; then
        err "Could not connect via SSH after ${MAX_RETRIES} attempts."
        exit 1
    fi
    info "  Attempt ${RETRY}/${MAX_RETRIES}, retrying in 10s..."
    sleep 10
done
ok "SSH connection established."

# =============================================================================
# Step 2: Wait for Docker (user-data provisioning)
# =============================================================================
info "[2/7] Waiting for user-data provisioning to complete..."
MAX_RETRIES=30
RETRY=0
until $SSH_CMD "docker --version" &>/dev/null; do
    RETRY=$((RETRY + 1))
    if [[ $RETRY -ge $MAX_RETRIES ]]; then
        err "Docker not available after ${MAX_RETRIES} attempts."
        echo "  Check user-data log: ssh -i ${SSH_KEY} ec2-user@${PUBLIC_IP} 'cat /var/log/trading-bot-setup.log'"
        exit 1
    fi
    info "  Docker not ready yet, retrying in 10s (${RETRY}/${MAX_RETRIES})..."
    sleep 10
done
ok "Docker is installed."

# =============================================================================
# Step 3: Clone or update the repository
# =============================================================================
info "[3/7] Cloning repository..."
$SSH_CMD "
    if [[ -d ${REMOTE_DIR}/.git ]]; then
        echo '  -> Repo exists, pulling latest changes...'
        cd ${REMOTE_DIR}
        git fetch origin
        git checkout ${BRANCH}
        git pull origin ${BRANCH}
    else
        echo '  -> Cloning fresh...'
        sudo rm -rf ${REMOTE_DIR}/*
        git clone --branch ${BRANCH} ${REPO_URL} ${REMOTE_DIR}
    fi
"
ok "Repository ready."

# =============================================================================
# Step 4: Create .env on remote with RDS credentials
# =============================================================================
info "[4/7] Creating .env on remote with RDS credentials..."

# Copy the local .env as a base
$SCP_CMD "$ENV_FILE" "ec2-user@${PUBLIC_IP}:${REMOTE_DIR}/.env"

# Override DB settings and deployment flags with RDS values
$SSH_CMD "
    cd ${REMOTE_DIR}

    # Remove existing DB_* and deployment lines to avoid duplicates
    sed -i '/^DB_HOST=/d'          .env
    sed -i '/^DB_PORT=/d'          .env
    sed -i '/^DB_NAME=/d'          .env
    sed -i '/^DB_USER=/d'          .env
    sed -i '/^DB_PASSWORD=/d'      .env
    sed -i '/^DRY_RUN_GLOBAL=/d'   .env
    sed -i '/^PPI_SANDBOX=/d'      .env

    # Append RDS and deployment settings
    cat >> .env <<'ENVEOF'

# --- RDS PostgreSQL (set by deploy.sh) ---
DB_HOST=${RDS_ENDPOINT}
DB_PORT=${RDS_PORT}
DB_NAME=${RDS_DB_NAME}
DB_USER=${RDS_MASTER_USER}
DB_PASSWORD=${RDS_MASTER_PASSWORD}

# --- Deployment flags (set by deploy.sh) ---
DRY_RUN_GLOBAL=true
PPI_SANDBOX=false
ENVEOF
"
ok ".env configured with RDS endpoint."

# =============================================================================
# Step 5: Run database migrations against RDS
# =============================================================================
info "[5/7] Running database migrations against RDS..."

$SSH_CMD "
    cd ${REMOTE_DIR}

    export PGPASSWORD='${RDS_MASTER_PASSWORD}'
    RDS_HOST='${RDS_ENDPOINT}'
    RDS_PORT='${RDS_PORT}'
    RDS_USER='${RDS_MASTER_USER}'
    RDS_DB='${RDS_DB_NAME}'

    # Wait for RDS to accept connections (might still be warming up)
    MAX_RETRIES=12
    RETRY=0
    until psql -h \"\$RDS_HOST\" -p \"\$RDS_PORT\" -U \"\$RDS_USER\" -d \"\$RDS_DB\" -c 'SELECT 1;' &>/dev/null; do
        RETRY=\$((RETRY + 1))
        if [[ \$RETRY -ge \$MAX_RETRIES ]]; then
            echo 'ERROR: Cannot connect to RDS after multiple attempts.'
            exit 1
        fi
        echo \"  -> RDS not accepting connections yet, retrying in 10s (\${RETRY}/\${MAX_RETRIES})...\"
        sleep 10
    done
    echo '  -> RDS is accepting connections.'

    # Run migrations in order
    if [[ -f db/migrations/001_initial.sql ]]; then
        echo '  -> Running 001_initial.sql...'
        psql -h \"\$RDS_HOST\" -p \"\$RDS_PORT\" -U \"\$RDS_USER\" -d \"\$RDS_DB\" \
            -f db/migrations/001_initial.sql
    fi

    if [[ -f db/migrations/002_research_feedback.sql ]]; then
        echo '  -> Running 002_research_feedback.sql...'
        psql -h \"\$RDS_HOST\" -p \"\$RDS_PORT\" -U \"\$RDS_USER\" -d \"\$RDS_DB\" \
            -f db/migrations/002_research_feedback.sql
    fi

    echo '  -> Migrations complete.'
"
ok "Database migrations applied."

# =============================================================================
# Step 6: Build and start services (production compose — no postgres container)
# =============================================================================
info "[6/7] Building and starting services (docker-compose.prod.yml)..."
$SSH_CMD "
    cd ${REMOTE_DIR}

    if docker compose version &>/dev/null; then
        docker compose -f docker-compose.prod.yml up -d --build
    else
        docker-compose -f docker-compose.prod.yml up -d --build
    fi
"
ok "Services started."

# =============================================================================
# Step 7: Verify service health
# =============================================================================
info "[7/7] Verifying service health..."
sleep 5

HEALTH_OUTPUT=$($SSH_CMD "
    cd ${REMOTE_DIR}
    echo '--- Container Status ---'
    if docker compose version &>/dev/null; then
        docker compose -f docker-compose.prod.yml ps
    else
        docker-compose -f docker-compose.prod.yml ps
    fi
    echo ''
    echo '--- Docker Logs (last 20 lines) ---'
    if docker compose version &>/dev/null; then
        docker compose -f docker-compose.prod.yml logs --tail=20
    else
        docker-compose -f docker-compose.prod.yml logs --tail=20
    fi
")

echo "$HEALTH_OUTPUT"

RUNNING=$($SSH_CMD "docker ps --format '{{.Status}}' | grep -c 'Up' || true")
if [[ "$RUNNING" -eq 0 ]]; then
    warn "No containers appear to be running."
    echo "  Check logs: ${SSH_CMD} 'cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml logs'"
    exit 1
fi

echo ""
echo "========================================="
echo -e "  ${GREEN}Deploy Complete${NC}"
echo "========================================="
echo ""
echo "  ${RUNNING} container(s) running."
echo ""
echo "  Dashboard: http://${PUBLIC_IP}:9091"
echo "  SSH:       ssh -i ${SSH_KEY} ec2-user@${PUBLIC_IP}"
echo "  Logs:      ssh -i ${SSH_KEY} ec2-user@${PUBLIC_IP} 'cd ${REMOTE_DIR} && docker compose -f docker-compose.prod.yml logs -f'"
echo "  RDS:       ${RDS_ENDPOINT}:${RDS_PORT}"
echo "========================================="
