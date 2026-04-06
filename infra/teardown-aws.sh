#!/bin/bash
# =============================================================================
# teardown-aws.sh — Clean up all AWS resources created by setup-aws.sh
#
# Reads the state file and tears down resources in proper order:
#   1. Disassociate and release Elastic IP
#   2. Terminate EC2 instance (wait for full termination)
#   3. Delete RDS instance (skip final snapshot, can take 5+ min)
#   4. Delete RDS DB subnet group
#   5. Delete security groups (EC2 + RDS)
#   6. Delete key pair (if auto-created) + local .pem file
#   7. Remove state file
#
# Usage:
#   ./infra/teardown-aws.sh [--force]
#
# The --force flag skips the confirmation prompt.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${SCRIPT_DIR}/.aws-state"
FORCE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--force]"
            exit 0
            ;;
        *)
            err "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# -----------------------------------------------------------------------------
# Load state
# -----------------------------------------------------------------------------
if [[ ! -f "$STATE_FILE" ]]; then
    err "State file not found at ${STATE_FILE}"
    echo "  Nothing to tear down, or setup-aws.sh was not run from this directory."
    exit 1
fi

# shellcheck source=/dev/null
source "$STATE_FILE"

AWS="aws --region $REGION --output text"

echo ""
echo "========================================="
echo -e "  ${RED}Trading Bot — AWS Teardown${NC}"
echo "========================================="
echo ""
echo "  This will PERMANENTLY destroy the following resources:"
echo ""
echo "  EC2 Instance   : ${INSTANCE_ID:-none}"
echo "  Elastic IP     : ${PUBLIC_IP:-none} (${ALLOC_ID:-none})"
echo "  EC2 Sec. Group : ${EC2_SG_ID:-none}"
echo "  RDS Instance   : ${RDS_IDENTIFIER:-none}"
echo "  RDS Sec. Group : ${RDS_SG_ID:-none}"
echo "  DB Subnet Group: ${SUBNET_GROUP_NAME:-none}"
echo "  Key Pair       : ${KEY_NAME:-none} (auto-created: ${KEY_AUTO_CREATED:-false})"
echo "  Region         : ${REGION}"
echo ""

if [[ "$FORCE" != true ]]; then
    read -rp "Are you sure? Type 'yes' to proceed: " CONFIRM
    if [[ "$CONFIRM" != "yes" ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""

# =============================================================================
# Step 1: Disassociate Elastic IP
# =============================================================================
info "[1/8] Disassociating Elastic IP..."
if [[ -n "${ASSOC_ID:-}" && "$ASSOC_ID" != "None" ]]; then
    $AWS ec2 disassociate-address --association-id "$ASSOC_ID" 2>/dev/null || true
    ok "Disassociated."
else
    warn "No association to remove."
fi

# =============================================================================
# Step 2: Release Elastic IP
# =============================================================================
info "[2/8] Releasing Elastic IP ${PUBLIC_IP:-unknown}..."
if [[ -n "${ALLOC_ID:-}" && "$ALLOC_ID" != "None" ]]; then
    $AWS ec2 release-address --allocation-id "$ALLOC_ID" 2>/dev/null || true
    ok "Released."
else
    warn "No allocation to release."
fi

# =============================================================================
# Step 3: Terminate EC2 instance
# =============================================================================
info "[3/8] Terminating EC2 instance ${INSTANCE_ID:-unknown}..."
if [[ -n "${INSTANCE_ID:-}" && "$INSTANCE_ID" != "None" ]]; then
    $AWS ec2 terminate-instances --instance-ids "$INSTANCE_ID" > /dev/null 2>&1 || true
    info "Termination initiated. Waiting for instance to terminate..."
    aws --region "$REGION" ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" 2>/dev/null || true
    ok "Instance terminated."
else
    warn "No instance to terminate."
fi

# =============================================================================
# Step 4: Delete RDS instance (this can take 5+ minutes)
# =============================================================================
info "[4/8] Deleting RDS instance '${RDS_IDENTIFIER:-unknown}'..."
if [[ -n "${RDS_IDENTIFIER:-}" ]]; then
    # Check if the instance exists
    RDS_STATUS=$($AWS rds describe-db-instances \
        --db-instance-identifier "$RDS_IDENTIFIER" \
        --query "DBInstances[0].DBInstanceStatus" 2>/dev/null || true)

    if [[ -n "$RDS_STATUS" && "$RDS_STATUS" != "None" ]]; then
        $AWS rds delete-db-instance \
            --db-instance-identifier "$RDS_IDENTIFIER" \
            --skip-final-snapshot \
            --delete-automated-backups > /dev/null 2>&1 || true
        info "RDS deletion initiated. Waiting for RDS to be deleted (this can take 5+ minutes)..."
        aws --region "$REGION" rds wait db-instance-deleted \
            --db-instance-identifier "$RDS_IDENTIFIER" 2>/dev/null || true
        ok "RDS instance deleted."
    else
        warn "RDS instance '${RDS_IDENTIFIER}' not found or already deleted."
    fi
else
    warn "No RDS identifier in state file."
fi

# =============================================================================
# Step 5: Delete DB subnet group
# =============================================================================
info "[5/8] Deleting DB subnet group '${SUBNET_GROUP_NAME:-unknown}'..."
if [[ -n "${SUBNET_GROUP_NAME:-}" ]]; then
    $AWS rds delete-db-subnet-group \
        --db-subnet-group-name "$SUBNET_GROUP_NAME" 2>/dev/null || true
    ok "DB subnet group deleted."
else
    warn "No subnet group to delete."
fi

# =============================================================================
# Step 6: Delete security groups (EC2 + RDS)
# =============================================================================
info "[6/8] Deleting security groups..."

delete_sg() {
    local sg_id="$1"
    local sg_name="$2"

    if [[ -z "$sg_id" || "$sg_id" == "None" ]]; then
        warn "No ${sg_name} security group to delete."
        return
    fi

    RETRY=0
    MAX_RETRIES=6
    until $AWS ec2 delete-security-group --group-id "$sg_id" 2>/dev/null; do
        RETRY=$((RETRY + 1))
        if [[ $RETRY -ge $MAX_RETRIES ]]; then
            warn "Could not delete ${sg_name} security group ${sg_id} after ${MAX_RETRIES} attempts."
            echo "  Delete manually: aws ec2 delete-security-group --group-id ${sg_id} --region ${REGION}"
            return
        fi
        info "  SG ${sg_id} still in use, retrying in 10s (${RETRY}/${MAX_RETRIES})..."
        sleep 10
    done
    ok "Deleted ${sg_name} security group: ${sg_id}"
}

# Delete RDS SG first (it references EC2 SG), then EC2 SG
delete_sg "${RDS_SG_ID:-}" "RDS"
delete_sg "${EC2_SG_ID:-}" "EC2"

# =============================================================================
# Step 7: Delete key pair (if auto-created)
# =============================================================================
info "[7/8] Cleaning up key pair..."
if [[ "${KEY_AUTO_CREATED:-false}" == "true" ]]; then
    # Delete from AWS
    $AWS ec2 delete-key-pair --key-name "$KEY_NAME" 2>/dev/null || true
    ok "Deleted key pair '${KEY_NAME}' from AWS."

    # Delete local .pem file
    PEM_FILE="${SCRIPT_DIR}/${KEY_NAME}.pem"
    if [[ -f "$PEM_FILE" ]]; then
        rm -f "$PEM_FILE"
        ok "Deleted local PEM file: ${PEM_FILE}"
    fi
else
    info "Key pair '${KEY_NAME}' was user-provided, not deleting."
fi

# =============================================================================
# Step 8: Remove state file
# =============================================================================
info "[8/8] Removing state file..."
rm -f "$STATE_FILE"
ok "State file removed."

echo ""
echo "========================================="
echo -e "  ${GREEN}Teardown Complete${NC}"
echo "========================================="
echo "  All resources have been cleaned up."
echo "========================================="
