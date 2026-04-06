#!/bin/bash
# =============================================================================
# setup-aws.sh — Provision AWS infrastructure for the trading bot
#
# Creates: EC2 instance with Elastic IP, RDS PostgreSQL, security groups,
# and optionally a key pair.
#
# Usage:
#   ./infra/setup-aws.sh [--key-name my-key] [--region sa-east-1] [--instance-type t3.small]
#
# If --key-name is not provided, a key pair is created automatically and the
# .pem file is saved to infra/.
#
# Prerequisites:
#   - AWS CLI v2 installed and configured with valid credentials
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Colors for output
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# -----------------------------------------------------------------------------
# Default parameter values
# -----------------------------------------------------------------------------
REGION="sa-east-1"
INSTANCE_TYPE="t3.small"
KEY_NAME=""
KEY_AUTO_CREATED=false

# State file to persist resource IDs for teardown
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${SCRIPT_DIR}/.aws-state"

# RDS configuration
RDS_INSTANCE_CLASS="db.t3.micro"
RDS_STORAGE_GB=20
RDS_ENGINE_VERSION="16"
RDS_DB_NAME="trading"
RDS_MASTER_USER="trading"
RDS_IDENTIFIER="trading-bot-db"

# -----------------------------------------------------------------------------
# Parse command-line arguments
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)
            REGION="$2"
            shift 2
            ;;
        --key-name)
            KEY_NAME="$2"
            shift 2
            ;;
        --instance-type)
            INSTANCE_TYPE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--key-name <KEY_PAIR_NAME>] [--region sa-east-1] [--instance-type t3.small]"
            echo ""
            echo "If --key-name is omitted, a key pair 'trading-bot-key' is created automatically."
            exit 0
            ;;
        *)
            err "Unknown argument: $1"
            echo "Usage: $0 [--key-name <KEY_PAIR_NAME>] [--region sa-east-1] [--instance-type t3.small]"
            exit 1
            ;;
    esac
done

# Check for existing state file
if [[ -f "$STATE_FILE" ]]; then
    err "State file already exists at ${STATE_FILE}"
    echo "  If you want to re-provision, run ./infra/teardown-aws.sh first."
    exit 1
fi

echo ""
echo "========================================="
echo "  Trading Bot — AWS Infrastructure Setup"
echo "========================================="
echo ""
info "Region:        $REGION"
info "Instance type: $INSTANCE_TYPE"
if [[ -n "$KEY_NAME" ]]; then
    info "Key pair:      $KEY_NAME (user-provided)"
else
    info "Key pair:      trading-bot-key (will be auto-created)"
fi
echo ""

# Common AWS CLI flags
AWS="aws --region $REGION --output text"

# Generate RDS master password (alphanumeric, 24 chars — RDS does not allow / @ " in passwords)
RDS_MASTER_PASSWORD=$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 24)

# =============================================================================
# Step 1: Detect caller's public IP
# =============================================================================
info "[1/10] Detecting your public IP address..."
MY_IP=$(curl -s --max-time 10 https://ifconfig.me || curl -s --max-time 10 https://api.ipify.org || true)
if [[ -z "$MY_IP" ]]; then
    err "Could not detect public IP. Check your internet connection."
    exit 1
fi
ok "Your IP: ${MY_IP}"

# =============================================================================
# Step 2: Get the default VPC and subnets
# =============================================================================
info "[2/10] Looking up default VPC..."
VPC_ID=$($AWS ec2 describe-vpcs \
    --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" 2>/dev/null || true)

if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
    info "No default VPC found. Creating one..."
    VPC_ID=$($AWS ec2 create-default-vpc --query "Vpc.VpcId")
fi
ok "VPC: ${VPC_ID}"

# Get ALL default subnets (need at least 2 AZs for RDS subnet group)
SUBNET_IDS=($($AWS ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=default-for-az,Values=true" \
    --query "Subnets[*].SubnetId"))

if [[ ${#SUBNET_IDS[@]} -lt 2 ]]; then
    err "Need at least 2 subnets in different AZs for RDS. Found ${#SUBNET_IDS[@]}."
    exit 1
fi
ok "Found ${#SUBNET_IDS[@]} subnets: ${SUBNET_IDS[*]}"

# Use first subnet for EC2
EC2_SUBNET_ID="${SUBNET_IDS[0]}"

# =============================================================================
# Step 3: Create or use key pair
# =============================================================================
info "[3/10] Setting up SSH key pair..."
if [[ -z "$KEY_NAME" ]]; then
    KEY_NAME="trading-bot-key"
    PEM_FILE="${SCRIPT_DIR}/${KEY_NAME}.pem"

    # Check if key pair already exists in AWS
    EXISTING=$($AWS ec2 describe-key-pairs \
        --key-names "$KEY_NAME" \
        --query "KeyPairs[0].KeyName" 2>/dev/null || true)

    if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
        warn "Key pair '${KEY_NAME}' already exists in AWS."
        if [[ ! -f "$PEM_FILE" ]]; then
            err "But local .pem file not found at ${PEM_FILE}. Delete the key pair in AWS or provide --key-name."
            exit 1
        fi
        ok "Using existing key pair: ${KEY_NAME}"
    else
        info "Creating key pair '${KEY_NAME}'..."
        aws --region "$REGION" ec2 create-key-pair \
            --key-name "$KEY_NAME" \
            --key-type ed25519 \
            --query "KeyMaterial" \
            --output text > "$PEM_FILE"
        chmod 600 "$PEM_FILE"
        KEY_AUTO_CREATED=true
        ok "Key pair created. PEM saved to ${PEM_FILE}"
    fi
    SSH_KEY="$PEM_FILE"
else
    # User-provided key — expect it in ~/.ssh/
    SSH_KEY="$HOME/.ssh/${KEY_NAME}.pem"
    ok "Using user-provided key pair: ${KEY_NAME}"
fi

# =============================================================================
# Step 4: Create EC2 security group
# =============================================================================
info "[4/10] Creating EC2 security group 'trading-bot-ec2-sg'..."

EC2_SG_ID=$($AWS ec2 describe-security-groups \
    --filters "Name=group-name,Values=trading-bot-ec2-sg" "Name=vpc-id,Values=${VPC_ID}" \
    --query "SecurityGroups[0].GroupId" 2>/dev/null || true)

if [[ -z "$EC2_SG_ID" || "$EC2_SG_ID" == "None" ]]; then
    EC2_SG_ID=$($AWS ec2 create-security-group \
        --group-name trading-bot-ec2-sg \
        --description "EC2 security group for Trading Bot — SSH and dashboard" \
        --vpc-id "$VPC_ID" \
        --query "GroupId")
    ok "Created EC2 security group: ${EC2_SG_ID}"

    # SSH (port 22) from caller's IP
    $AWS ec2 authorize-security-group-ingress \
        --group-id "$EC2_SG_ID" \
        --protocol tcp \
        --port 22 \
        --cidr "${MY_IP}/32" > /dev/null
    ok "Allowed SSH (22) from ${MY_IP}/32"

    # Dashboard (port 9091) from caller's IP
    $AWS ec2 authorize-security-group-ingress \
        --group-id "$EC2_SG_ID" \
        --protocol tcp \
        --port 9091 \
        --cidr "${MY_IP}/32" > /dev/null
    ok "Allowed Dashboard (9091) from ${MY_IP}/32"
else
    warn "EC2 security group already exists: ${EC2_SG_ID}"
fi

# =============================================================================
# Step 5: Create RDS security group
# =============================================================================
info "[5/10] Creating RDS security group 'trading-bot-rds-sg'..."

RDS_SG_ID=$($AWS ec2 describe-security-groups \
    --filters "Name=group-name,Values=trading-bot-rds-sg" "Name=vpc-id,Values=${VPC_ID}" \
    --query "SecurityGroups[0].GroupId" 2>/dev/null || true)

if [[ -z "$RDS_SG_ID" || "$RDS_SG_ID" == "None" ]]; then
    RDS_SG_ID=$($AWS ec2 create-security-group \
        --group-name trading-bot-rds-sg \
        --description "RDS security group for Trading Bot — PostgreSQL from EC2 only" \
        --vpc-id "$VPC_ID" \
        --query "GroupId")
    ok "Created RDS security group: ${RDS_SG_ID}"

    # PostgreSQL (5432) from EC2 security group only
    $AWS ec2 authorize-security-group-ingress \
        --group-id "$RDS_SG_ID" \
        --protocol tcp \
        --port 5432 \
        --source-group "$EC2_SG_ID" > /dev/null
    ok "Allowed PostgreSQL (5432) from EC2 security group ${EC2_SG_ID}"
else
    warn "RDS security group already exists: ${RDS_SG_ID}"
fi

# =============================================================================
# Step 6: Create RDS DB subnet group
# =============================================================================
info "[6/10] Creating RDS DB subnet group..."

SUBNET_GROUP_NAME="trading-bot-subnet-group"

EXISTING_SUBNET_GROUP=$($AWS rds describe-db-subnet-groups \
    --db-subnet-group-name "$SUBNET_GROUP_NAME" \
    --query "DBSubnetGroups[0].DBSubnetGroupName" 2>/dev/null || true)

if [[ -z "$EXISTING_SUBNET_GROUP" || "$EXISTING_SUBNET_GROUP" == "None" ]]; then
    # Build subnet-ids argument
    SUBNET_ARGS=""
    for sid in "${SUBNET_IDS[@]}"; do
        SUBNET_ARGS="${SUBNET_ARGS} ${sid}"
    done

    $AWS rds create-db-subnet-group \
        --db-subnet-group-name "$SUBNET_GROUP_NAME" \
        --db-subnet-group-description "Subnets for Trading Bot RDS instance" \
        --subnet-ids ${SUBNET_ARGS} > /dev/null
    ok "Created DB subnet group: ${SUBNET_GROUP_NAME}"
else
    warn "DB subnet group already exists: ${SUBNET_GROUP_NAME}"
fi

# =============================================================================
# Step 7: Create RDS PostgreSQL instance
# =============================================================================
info "[7/10] Creating RDS PostgreSQL instance '${RDS_IDENTIFIER}'..."
info "  Engine: postgres ${RDS_ENGINE_VERSION}, Class: ${RDS_INSTANCE_CLASS}, Storage: ${RDS_STORAGE_GB}GB gp3"
info "  This will take 5-10 minutes..."

EXISTING_RDS=$($AWS rds describe-db-instances \
    --db-instance-identifier "$RDS_IDENTIFIER" \
    --query "DBInstances[0].DBInstanceIdentifier" 2>/dev/null || true)

if [[ -z "$EXISTING_RDS" || "$EXISTING_RDS" == "None" ]]; then
    $AWS rds create-db-instance \
        --db-instance-identifier "$RDS_IDENTIFIER" \
        --db-instance-class "$RDS_INSTANCE_CLASS" \
        --engine postgres \
        --engine-version "$RDS_ENGINE_VERSION" \
        --allocated-storage "$RDS_STORAGE_GB" \
        --storage-type gp3 \
        --db-name "$RDS_DB_NAME" \
        --master-username "$RDS_MASTER_USER" \
        --master-user-password "$RDS_MASTER_PASSWORD" \
        --vpc-security-group-ids "$RDS_SG_ID" \
        --db-subnet-group-name "$SUBNET_GROUP_NAME" \
        --no-multi-az \
        --no-publicly-accessible \
        --backup-retention-period 7 \
        --storage-encrypted \
        --tags "Key=Name,Value=trading-bot-rds" \
        > /dev/null
    ok "RDS creation initiated."
else
    warn "RDS instance '${RDS_IDENTIFIER}' already exists."
fi

# Wait for RDS to become available
info "Waiting for RDS to become available (this can take 5-10 minutes)..."
aws --region "$REGION" rds wait db-instance-available \
    --db-instance-identifier "$RDS_IDENTIFIER"
ok "RDS instance is available."

# Get the RDS endpoint
RDS_ENDPOINT=$($AWS rds describe-db-instances \
    --db-instance-identifier "$RDS_IDENTIFIER" \
    --query "DBInstances[0].Endpoint.Address")
RDS_PORT=$($AWS rds describe-db-instances \
    --db-instance-identifier "$RDS_IDENTIFIER" \
    --query "DBInstances[0].Endpoint.Port")
ok "RDS endpoint: ${RDS_ENDPOINT}:${RDS_PORT}"

# =============================================================================
# Step 8: Resolve AMI and launch EC2 instance
# =============================================================================
info "[8/10] Resolving latest Amazon Linux 2023 AMI..."
AMI_ID=$($AWS ssm get-parameters \
    --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query "Parameters[0].Value")
ok "AMI: ${AMI_ID}"

info "Launching EC2 instance..."

USER_DATA_FILE="${SCRIPT_DIR}/user-data.sh"
if [[ ! -f "$USER_DATA_FILE" ]]; then
    err "user-data.sh not found at ${USER_DATA_FILE}"
    exit 1
fi

INSTANCE_ID=$($AWS ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$EC2_SG_ID" \
    --subnet-id "$EC2_SUBNET_ID" \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3}" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=trading-bot}]" \
    --user-data "file://${USER_DATA_FILE}" \
    --associate-public-ip-address \
    --query "Instances[0].InstanceId")

ok "Instance launched: ${INSTANCE_ID}"
info "Waiting for instance to be running..."
aws --region "$REGION" ec2 wait instance-running --instance-ids "$INSTANCE_ID"
ok "Instance is running."

# =============================================================================
# Step 9: Allocate and associate Elastic IP
# =============================================================================
info "[9/10] Allocating Elastic IP..."
ALLOC_ID=$($AWS ec2 allocate-address \
    --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=trading-bot-eip}]" \
    --query "AllocationId")

ASSOC_ID=$($AWS ec2 associate-address \
    --instance-id "$INSTANCE_ID" \
    --allocation-id "$ALLOC_ID" \
    --query "AssociationId")

PUBLIC_IP=$($AWS ec2 describe-addresses \
    --allocation-ids "$ALLOC_ID" \
    --query "Addresses[0].PublicIp")
ok "Elastic IP: ${PUBLIC_IP}"

# =============================================================================
# Step 10: Save state and print summary
# =============================================================================
info "[10/10] Saving state for teardown..."
cat > "$STATE_FILE" <<EOF
# Auto-generated by setup-aws.sh — used by teardown-aws.sh and deploy.sh
# DO NOT commit this file to version control.
REGION=${REGION}
INSTANCE_ID=${INSTANCE_ID}
EC2_SG_ID=${EC2_SG_ID}
RDS_SG_ID=${RDS_SG_ID}
ALLOC_ID=${ALLOC_ID}
ASSOC_ID=${ASSOC_ID}
PUBLIC_IP=${PUBLIC_IP}
KEY_NAME=${KEY_NAME}
KEY_AUTO_CREATED=${KEY_AUTO_CREATED}
RDS_IDENTIFIER=${RDS_IDENTIFIER}
RDS_ENDPOINT=${RDS_ENDPOINT}
RDS_PORT=${RDS_PORT}
RDS_DB_NAME=${RDS_DB_NAME}
RDS_MASTER_USER=${RDS_MASTER_USER}
RDS_MASTER_PASSWORD=${RDS_MASTER_PASSWORD}
SUBNET_GROUP_NAME=${SUBNET_GROUP_NAME}
EOF
chmod 600 "$STATE_FILE"
ok "State saved to ${STATE_FILE}"

echo ""
echo "========================================="
echo -e "  ${GREEN}Setup Complete${NC}"
echo "========================================="
echo ""
echo "  EC2 Instance : ${INSTANCE_ID}"
echo "  Public IP    : ${PUBLIC_IP}"
echo "  Region       : ${REGION}"
echo ""
echo "  RDS Endpoint : ${RDS_ENDPOINT}"
echo "  RDS Port     : ${RDS_PORT}"
echo "  RDS Database : ${RDS_DB_NAME}"
echo "  RDS User     : ${RDS_MASTER_USER}"
echo "  RDS Password : (saved in ${STATE_FILE})"
echo ""
echo "  SSH command:"
echo "    ssh -i ${SSH_KEY} ec2-user@${PUBLIC_IP}"
echo ""
echo "  Dashboard will be at:"
echo "    http://${PUBLIC_IP}:9091"
echo ""
echo "  NOTE: Wait 2-3 minutes for user-data to finish installing Docker."
echo "  You can check progress with:"
echo "    ssh -i ${SSH_KEY} ec2-user@${PUBLIC_IP} 'tail -f /var/log/trading-bot-setup.log'"
echo ""
echo "  Next step: run ./infra/deploy.sh to deploy the application."
echo "========================================="
