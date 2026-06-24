#!/usr/bin/env bash
# relay-up.sh — resume the Relay Hub by scaling its ECS service back up.
#
# Reverses relay-down.sh. Brings the Fargate service back to its normal task
# count; the ALB/DNS/data were never removed, so the dashboard returns at the
# same URL once tasks pass health checks (~1-2 min).
#
# Usage:
#   ./scripts/relay-up.sh            # scale to default count
#   RELAY_DESIRED=1 ./scripts/relay-up.sh
#
# Env (optional):
#   AWS_REGION       — default us-east-1
#   RELAY_CLUSTER    — ECS cluster name (default: relay-hub)
#   RELAY_SERVICE    — ECS service name (default: relay-hub)
#   RELAY_DESIRED    — task count to restore (default: 2)
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${RELAY_CLUSTER:-relay-hub}"
SERVICE="${RELAY_SERVICE:-relay-hub}"
DESIRED="${RELAY_DESIRED:-2}"

echo "=== Relay up (scale to ${DESIRED}) ===" >&2
echo "Region:  ${AWS_REGION}" >&2
echo "Cluster: ${CLUSTER}" >&2
echo "Service: ${SERVICE}" >&2

CURRENT="$(aws ecs describe-services \
  --cluster "${CLUSTER}" --services "${SERVICE}" --region "${AWS_REGION}" \
  --query 'services[0].desiredCount' --output text 2>/dev/null || echo "NONE")"

if [ "${CURRENT}" = "NONE" ] || [ "${CURRENT}" = "None" ]; then
  echo "Service '${SERVICE}' not found in cluster '${CLUSTER}'." >&2
  echo "If the stack was destroyed, redeploy with ./scripts/relay-deploy.sh instead." >&2
  exit 1
fi

aws ecs update-service \
  --cluster "${CLUSTER}" --service "${SERVICE}" \
  --desired-count "${DESIRED}" --region "${AWS_REGION}" \
  --query 'service.{name:serviceName,desired:desiredCount}' --output json >&2

echo "" >&2
echo "Scale-to-${DESIRED} requested. Dashboard returns once tasks are healthy (~1-2 min)." >&2
