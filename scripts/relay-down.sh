#!/usr/bin/env bash
# relay-down.sh — pause the Relay Hub overnight by scaling its ECS service to 0.
#
# This stops the Fargate compute (the main running cost) WITHOUT destroying the
# stack: the ALB, DNS name, DynamoDB tables (contacts, fleet, schedule), SNS
# topics and EventBridge bus all stay intact. Bring it back with relay-up.sh.
#
# It does NOT touch any IAM/role/VPC resources and creates nothing — purely a
# desired-count change, so it's safe in locked-down accounts.
#
# Usage:
#   ./scripts/relay-down.sh
#
# Env (optional):
#   AWS_REGION       — default us-east-1
#   RELAY_CLUSTER    — ECS cluster name (default: relay-hub)
#   RELAY_SERVICE    — ECS service name (default: relay-hub)
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${RELAY_CLUSTER:-relay-hub}"
SERVICE="${RELAY_SERVICE:-relay-hub}"

echo "=== Relay down (scale to 0) ===" >&2
echo "Region:  ${AWS_REGION}" >&2
echo "Cluster: ${CLUSTER}" >&2
echo "Service: ${SERVICE}" >&2

CURRENT="$(aws ecs describe-services \
  --cluster "${CLUSTER}" --services "${SERVICE}" --region "${AWS_REGION}" \
  --query 'services[0].desiredCount' --output text 2>/dev/null || echo "NONE")"

if [ "${CURRENT}" = "NONE" ] || [ "${CURRENT}" = "None" ]; then
  echo "Service '${SERVICE}' not found in cluster '${CLUSTER}'. Nothing to do." >&2
  exit 0
fi

echo "Current desiredCount: ${CURRENT}" >&2

if [ "${CURRENT}" = "0" ]; then
  echo "Already scaled to 0. Nothing to do." >&2
  exit 0
fi

aws ecs update-service \
  --cluster "${CLUSTER}" --service "${SERVICE}" \
  --desired-count 0 --region "${AWS_REGION}" \
  --query 'service.{name:serviceName,desired:desiredCount}' --output json >&2

echo "" >&2
echo "Scale-to-0 requested. Tasks will drain shortly." >&2
echo "Bring it back with: ./scripts/relay-up.sh" >&2
