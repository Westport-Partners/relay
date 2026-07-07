#!/usr/bin/env bash
# relay-teardown-direct.sh — tear down a CloudFormation / BYOR-deployed Relay install.
#
# WHY THIS EXISTS
# ---------------
# `cdk destroy` requires `iam:PassRole` (the same permission that blocks `cdk deploy`
# on locked-down BYOR accounts). `relay-teardown-cli.sh` only removes resources
# created by relay-provision-cli.sh — it does NOT apply to CloudFormation stacks.
# BYOR operators therefore had no scripted teardown path.
#
# This script implements the manual sequence documented in prompts/deploy-byor.md:
#   1. Delete the compute stack (ECS service, ALB, security groups)
#   2. Delete the data stack (SNS, SQS, EventBridge — DynamoDB is RETAINED by policy)
#   3. Delete the retained DynamoDB table (it survives stack deletion by design)
#   4. (Opt-in) Delete ECR images — DESTRUCTIVE, requires --purge-ecr / RELAY_PURGE_ECR=1
#
# Stack names and the DynamoDB table name are derived from the same env-var surface
# as relay-deploy-direct.sh / relay-context.sh:
#   RELAY_DEPLOY_TYPE  team | federated-hub (default: team)
#   RELAY_TEAM_NAME    team id (required for team topology)
#   RELAY_ORG_ID       AWS org id (required for federated-hub topology)
#   AWS_REGION         target region (default: us-east-1)
#
# DESTRUCTIVE OPERATION — confirmation is required unless RELAY_FORCE=1 is set.
# All stack deletions are idempotent: already-absent stacks are skipped gracefully.
#
# Usage:
#   RELAY_TEAM_NAME=<team> [AWS_REGION=us-east-1] [RELAY_FORCE=1] \
#     ./scripts/relay-teardown-direct.sh [--purge-ecr]
#
#   # Federated-hub teardown:
#   RELAY_DEPLOY_TYPE=federated-hub RELAY_ORG_ID=<org-id> [AWS_REGION=us-east-1] \
#     ./scripts/relay-teardown-direct.sh [--purge-ecr]
set -euo pipefail

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
_purge_ecr="${RELAY_PURGE_ECR:-0}"
for _arg in "$@"; do
  case "${_arg}" in
    --purge-ecr) _purge_ecr=1 ;;
    *) echo "ERROR: unknown argument '${_arg}'" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Env vars (mirror relay-context.sh surface; no CDK context needed here)
# ---------------------------------------------------------------------------
RELAY_DEPLOY_TYPE="${RELAY_DEPLOY_TYPE:-team}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="${AWS_REGION}"

# Normalize deprecated topology aliases (same logic as relay-context.sh)
case "${RELAY_DEPLOY_TYPE}" in
  standalone|node) _topology="team" ;;
  hub)             _topology="federated-hub" ;;
  team|federated-hub) _topology="${RELAY_DEPLOY_TYPE}" ;;
  *) echo "ERROR: unknown RELAY_DEPLOY_TYPE='${RELAY_DEPLOY_TYPE}' (team|federated-hub)" >&2; exit 1 ;;
esac

# Topology-specific validation and resource derivation
case "${_topology}" in
  team)
    RELAY_TEAM_NAME="${RELAY_TEAM_NAME:-}"
    if [ -z "${RELAY_TEAM_NAME}" ]; then
      echo "ERROR: RELAY_TEAM_NAME is required for team topology." >&2
      exit 1
    fi
    TABLE="relay-${RELAY_TEAM_NAME}"
    # team topology: compute + data stacks; delete compute first (it imports data),
    # then data.
    _STACKS="RelayComputeStack RelayDataStack"
    ;;
  federated-hub)
    RELAY_ORG_ID="${RELAY_ORG_ID:-}"
    if [ -z "${RELAY_ORG_ID}" ]; then
      echo "ERROR: RELAY_ORG_ID is required for federated-hub topology." >&2
      exit 1
    fi
    # The hub's data stack creates a relay-hub table (the hub's own state store).
    TABLE="relay-hub"
    # federated-hub: federation stack depends on compute; delete federation first,
    # then compute, then data.
    _STACKS="RelayFederationStack RelayComputeStack RelayDataStack"
    ;;
esac

ECR_REPO="relay-hub"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "=== Relay direct teardown ===" >&2
echo "Account: ${ACCOUNT_ID} / region: ${AWS_REGION} / topology: ${_topology}" >&2
echo "Stacks (deletion order): ${_STACKS}" >&2
echo "DynamoDB table (retained): ${TABLE}" >&2
[ "${_purge_ecr}" = "1" ] && echo "ECR purge: ENABLED (--purge-ecr)" >&2

# ---------------------------------------------------------------------------
# Confirmation prompt — mirrors relay-teardown-cli.sh guard pattern
# ---------------------------------------------------------------------------
if [ "${RELAY_FORCE:-0}" != "1" ]; then
  echo "" >&2
  echo "This will DELETE the following resources in ${AWS_REGION}:" >&2
  for _s in ${_STACKS}; do
    echo "  CloudFormation stack: ${_s}" >&2
  done
  echo "  DynamoDB table: ${TABLE}  (RETAIN policy — must be removed explicitly)" >&2
  [ "${_purge_ecr}" = "1" ] && echo "  ECR repository images: ${ECR_REPO} (all images)" >&2
  echo "" >&2
  echo "All incident, contact, and schedule data WILL BE DESTROYED and cannot be recovered." >&2
  printf "Type the DynamoDB table name to confirm: " >&2
  read -r _confirm
  if [ "${_confirm}" != "${TABLE}" ]; then
    echo "  Confirmation did not match — teardown aborted." >&2
    echo "  (Re-run with RELAY_FORCE=1 to skip the prompt, or delete by hand.)" >&2
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Helper: delete one CloudFormation stack, waiting for completion.
# Idempotent — already-absent stacks (DELETE_COMPLETE or never existed) are
# skipped with a note. A stack stuck in DELETE_FAILED is reported and the
# script exits so the operator can investigate manually.
# ---------------------------------------------------------------------------
_delete_stack() {
  local _stack="$1"
  local _status
  _status="$(aws cloudformation describe-stacks \
    --stack-name "${_stack}" \
    --region "${AWS_REGION}" \
    --query 'Stacks[0].StackStatus' \
    --output text 2>/dev/null || echo "DOES_NOT_EXIST")"

  case "${_status}" in
    DOES_NOT_EXIST|DELETE_COMPLETE)
      echo "  CloudFormation ${_stack}: not found (skipping)" >&2
      return 0
      ;;
    DELETE_IN_PROGRESS)
      echo "  CloudFormation ${_stack}: delete already in progress — waiting..." >&2
      ;;
    DELETE_FAILED)
      echo "ERROR: CloudFormation ${_stack} is in DELETE_FAILED — manual intervention required." >&2
      echo "       Check stack events: aws cloudformation describe-stack-events --stack-name ${_stack}" >&2
      exit 1
      ;;
    *)
      echo "  CloudFormation ${_stack}: deleting (current status: ${_status})..." >&2
      aws cloudformation delete-stack \
        --stack-name "${_stack}" \
        --region "${AWS_REGION}"
      ;;
  esac

  echo "  CloudFormation ${_stack}: waiting for deletion to complete..." >&2
  if ! aws cloudformation wait stack-delete-complete \
    --stack-name "${_stack}" \
    --region "${AWS_REGION}"; then
    # wait exits non-zero when the stack ends in DELETE_FAILED
    _final="$(aws cloudformation describe-stacks \
      --stack-name "${_stack}" \
      --region "${AWS_REGION}" \
      --query 'Stacks[0].StackStatus' \
      --output text 2>/dev/null || echo "GONE")"
    if [ "${_final}" = "GONE" ] || [ "${_final}" = "DOES_NOT_EXIST" ]; then
      echo "  CloudFormation ${_stack}: deleted" >&2
      return 0
    fi
    echo "ERROR: CloudFormation ${_stack} deletion ended in ${_final}." >&2
    echo "       Check stack events: aws cloudformation describe-stack-events --stack-name ${_stack}" >&2
    exit 1
  fi
  echo "  CloudFormation ${_stack}: deleted" >&2
}

# ---------------------------------------------------------------------------
# Steps 1 & 2 (and pre-step for federated-hub): delete stacks in dependency
# order — compute before data; federation before compute for federated-hub.
# ---------------------------------------------------------------------------
for _stack in ${_STACKS}; do
  _delete_stack "${_stack}"
done

# ---------------------------------------------------------------------------
# Step 3: DynamoDB table — RETAIN deletion policy means it survives stack
# deletion; it must be removed explicitly if the operator wants it gone.
# ---------------------------------------------------------------------------
if aws dynamodb describe-table --table-name "${TABLE}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  aws dynamodb delete-table --table-name "${TABLE}" --region "${AWS_REGION}" >/dev/null
  echo "  DynamoDB ${TABLE}: delete requested (table will drain shortly)" >&2
else
  echo "  DynamoDB ${TABLE}: not found (skipping)" >&2
fi

# ---------------------------------------------------------------------------
# Step 4 (opt-in): ECR images. DESTRUCTIVE — all tagged and untagged images
# in the repository are deleted. The repository itself is left intact (it was
# not created by Relay's CDK stacks; callers typically pre-provision it).
# Skipped unless --purge-ecr or RELAY_PURGE_ECR=1.
# ---------------------------------------------------------------------------
if [ "${_purge_ecr}" = "1" ]; then
  echo "" >&2
  echo "=== ECR purge (DESTRUCTIVE — all images in ${ECR_REPO}) ===" >&2
  _image_ids="$(aws ecr list-images \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --query 'imageIds[*]' \
    --output json 2>/dev/null || echo "[]")"

  if [ "${_image_ids}" = "[]" ] || [ -z "${_image_ids}" ]; then
    echo "  ECR ${ECR_REPO}: no images found (skipping)" >&2
  else
    aws ecr batch-delete-image \
      --repository-name "${ECR_REPO}" \
      --region "${AWS_REGION}" \
      --image-ids "${_image_ids}" >/dev/null
    echo "  ECR ${ECR_REPO}: images deleted" >&2
  fi
else
  echo "" >&2
  echo "ECR images NOT purged (pass --purge-ecr or RELAY_PURGE_ECR=1 to delete all images in ${ECR_REPO})." >&2
fi

echo "" >&2
echo "Teardown complete." >&2
echo "To redeploy: scripts/relay-deploy-direct.sh (see prompts/deploy-byor.md)." >&2
