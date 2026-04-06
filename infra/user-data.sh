#!/bin/bash
# =============================================================================
# user-data.sh — EC2 first-boot provisioning for the trading bot instance
#
# This script runs as root via EC2 User Data on Amazon Linux 2023.
# It installs Docker, docker-compose plugin, PostgreSQL 16 client tools,
# and prepares the directory structure for the trading bot deployment.
# =============================================================================
set -euo pipefail

LOG="/var/log/trading-bot-setup.log"
exec > >(tee -a "$LOG") 2>&1

echo "========================================="
echo "Trading Bot — EC2 User Data Setup"
echo "Started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================="

# -----------------------------------------------------------------------------
# 1. System updates
# -----------------------------------------------------------------------------
echo "[1/7] Updating system packages..."
dnf update -y

# -----------------------------------------------------------------------------
# 2. Install Docker
# -----------------------------------------------------------------------------
echo "[2/7] Installing Docker..."
dnf install -y docker

# Enable and start the Docker daemon so it survives reboots
systemctl enable docker
systemctl start docker

# -----------------------------------------------------------------------------
# 3. Install docker-compose plugin
# -----------------------------------------------------------------------------
echo "[3/7] Installing docker-compose plugin..."

if dnf list docker-compose-plugin &>/dev/null 2>&1; then
    dnf install -y docker-compose-plugin
else
    COMPOSE_VERSION="v2.27.1"
    ARCH=$(uname -m)
    curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
        -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    mkdir -p /usr/local/lib/docker/cli-plugins
    ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
fi

# -----------------------------------------------------------------------------
# 4. Add ec2-user to the docker group
# -----------------------------------------------------------------------------
echo "[4/7] Adding ec2-user to docker group..."
usermod -aG docker ec2-user

# -----------------------------------------------------------------------------
# 5. Install PostgreSQL 16 client (for running migrations against RDS)
# -----------------------------------------------------------------------------
echo "[5/7] Installing PostgreSQL 16 client..."

# Amazon Linux 2023 may not have pg16 in default repos — add the official repo
if ! dnf list postgresql16 &>/dev/null 2>&1; then
    dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm 2>/dev/null || true
fi

# Try postgresql16 first, fall back to whatever is available
if dnf list postgresql16 &>/dev/null 2>&1; then
    dnf install -y postgresql16
else
    echo "  -> postgresql16 not available, installing default postgresql..."
    dnf install -y postgresql15
fi

# Verify psql is available
if command -v psql &>/dev/null; then
    echo "  -> psql version: $(psql --version)"
else
    echo "  -> WARNING: psql not found in PATH after installation."
fi

# -----------------------------------------------------------------------------
# 6. Install git
# -----------------------------------------------------------------------------
echo "[6/7] Installing git..."
dnf install -y git

# -----------------------------------------------------------------------------
# 7. Create the application directory
# -----------------------------------------------------------------------------
echo "[7/7] Creating /opt/trading-bot directory..."
mkdir -p /opt/trading-bot
chown ec2-user:ec2-user /opt/trading-bot

echo "========================================="
echo "Trading Bot — EC2 User Data Setup COMPLETE"
echo "Finished: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================="
