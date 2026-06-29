#!/usr/bin/env bash
# relay-provision-cli.sh — provision Relay's data plane + alarm ingest using ONLY
# the AWS CLI. No CDK, no CloudFormation, no bootstrap, no iam:PassRole.
#
# WHY THIS EXISTS
# ---------------
# The lowest-friction way to evaluate Relay is to create the few stateful AWS
# resources it needs, then run the container (or the Python process) locally
# against them — no ECS, no VPC, no IAM role creation. This script creates
# exactly those resources with plain `aws` calls, so it works in accounts that
# deny iam:CreateRole / iam:PassRole / ec2:CreateVpc and forbid CDK bootstrap.
# It mirrors what RelayDataStack and the ingest half of RelayComputeStack create,
# so you can later adopt the full CDK deploy without recreating data.
#
# Creates (all idempotent — safe to re-run):
#   DynamoDB  relay-<team>            single table: pk/sk, PAY_PER_REQUEST,
#                                     SSE, PITR, TTL=ttl, stream NEW_AND_OLD_IMAGES,
#                                     GSIs incident-status-index + incident-all-index
#   SNS       relay-<team>-paging               team on-call paging topic
#   SNS       relay-<team>-central-paging       central paging topic
#   SQS       relay-hub-ingest-dlq              poison-message DLQ (14d retention)
#   SQS       relay-hub-ingest                  alarm ingest queue (redrive → DLQ)
#   EventBridge rule relay-cloudwatch-alarm     CloudWatch ALARM state-change → queue
#
# Usage:
#   RELAY_TEAM_NAME=<team> [AWS_REGION=us-east-1] ./scripts/relay-provision-cli.sh
#
# On success it prints the env vars to export before running Relay locally
# (see docs/local-dev.md).
set -euo pipefail

RELAY_TEAM_NAME="${RELAY_TEAM_NAME:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="${AWS_REGION}"

if [ -z "${RELAY_TEAM_NAME}" ]; then
  echo "ERROR: RELAY_TEAM_NAME is required (names the table relay-<team>)." >&2
  exit 1
fi

TABLE="relay-${RELAY_TEAM_NAME}"
PAGING_TOPIC="relay-${RELAY_TEAM_NAME}-paging"
CENTRAL_TOPIC="relay-${RELAY_TEAM_NAME}-central-paging"
INGEST_QUEUE="relay-hub-ingest"
INGEST_DLQ="relay-hub-ingest-dlq"
ALARM_RULE="relay-cloudwatch-alarm"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Provisioning Relay data plane in account ${ACCOUNT_ID} / region ${AWS_REGION}" >&2

# ----------------------------------------------------------------------------
# 1. DynamoDB table (with both GSIs created up-front — matches RelayDataStack).
# ----------------------------------------------------------------------------
if aws dynamodb describe-table --table-name "${TABLE}" >/dev/null 2>&1; then
  echo "  DynamoDB ${TABLE}: exists (skipping create)" >&2
else
  echo "  DynamoDB ${TABLE}: creating..." >&2
  aws dynamodb create-table \
    --table-name "${TABLE}" \
    --billing-mode PAY_PER_REQUEST \
    --sse-specification Enabled=true \
    --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \
    --attribute-definitions \
        AttributeName=pk,AttributeType=S \
        AttributeName=sk,AttributeType=S \
        AttributeName=gsi_open_pk,AttributeType=S \
        AttributeName=gsi_all_pk,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
    --key-schema \
        AttributeName=pk,KeyType=HASH \
        AttributeName=sk,KeyType=RANGE \
    --global-secondary-indexes \
        'IndexName=incident-status-index,KeySchema=[{AttributeName=gsi_open_pk,KeyType=HASH},{AttributeName=created_at,KeyType=RANGE}],Projection={ProjectionType=ALL}' \
        'IndexName=incident-all-index,KeySchema=[{AttributeName=gsi_all_pk,KeyType=HASH},{AttributeName=created_at,KeyType=RANGE}],Projection={ProjectionType=ALL}' \
    >/dev/null
  echo "  DynamoDB ${TABLE}: waiting for ACTIVE..." >&2
  aws dynamodb wait table-exists --table-name "${TABLE}"
fi

# PITR + TTL are separate API calls (idempotent — re-applying the same value is a no-op).
aws dynamodb update-continuous-backups \
  --table-name "${TABLE}" \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true >/dev/null 2>&1 || \
  echo "  (PITR already enabled or pending)" >&2

if [ "$(aws dynamodb describe-time-to-live --table-name "${TABLE}" \
        --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null)" != "ENABLED" ]; then
  aws dynamodb update-time-to-live --table-name "${TABLE}" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" >/dev/null 2>&1 || \
    echo "  (TTL enable pending — re-run later if it did not take)" >&2
fi

TABLE_ARN="$(aws dynamodb describe-table --table-name "${TABLE}" \
  --query 'Table.TableArn' --output text)"

# ----------------------------------------------------------------------------
# 2. SNS paging topics (create-topic is idempotent: returns the ARN if it exists).
# ----------------------------------------------------------------------------
PAGING_TOPIC_ARN="$(aws sns create-topic --name "${PAGING_TOPIC}" --output text --query TopicArn)"
echo "  SNS ${PAGING_TOPIC}: ${PAGING_TOPIC_ARN}" >&2
CENTRAL_TOPIC_ARN="$(aws sns create-topic --name "${CENTRAL_TOPIC}" --output text --query TopicArn)"
echo "  SNS ${CENTRAL_TOPIC}: ${CENTRAL_TOPIC_ARN}" >&2

# ----------------------------------------------------------------------------
# 3. SQS ingest DLQ + queue with redrive (matches RelayComputeStack ingest half).
# ----------------------------------------------------------------------------
DLQ_URL="$(aws sqs create-queue --queue-name "${INGEST_DLQ}" \
  --attributes MessageRetentionPeriod=1209600 \
  --output text --query QueueUrl)"
DLQ_ARN="$(aws sqs get-queue-attributes --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)"
echo "  SQS ${INGEST_DLQ}: ${DLQ_URL}" >&2

# RedrivePolicy is itself a JSON string, so the whole --attributes value must be a
# JSON document (file://). AWS CLI shorthand (Key=Value) cannot carry nested JSON.
_ATTRS_TMP="$(mktemp)"
trap 'rm -f "${_ATTRS_TMP}"' EXIT
python3 -c "
import json, sys
print(json.dumps({
  'VisibilityTimeout': '60',
  'MessageRetentionPeriod': '345600',
  'RedrivePolicy': json.dumps({'deadLetterTargetArn': sys.argv[1], 'maxReceiveCount': 5}),
}))
" "${DLQ_ARN}" > "${_ATTRS_TMP}"
QUEUE_URL="$(aws sqs create-queue --queue-name "${INGEST_QUEUE}" \
  --attributes "file://${_ATTRS_TMP}" \
  --output text --query QueueUrl)"
rm -f "${_ATTRS_TMP}"
trap - EXIT
QUEUE_ARN="$(aws sqs get-queue-attributes --queue-url "${QUEUE_URL}" \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)"
echo "  SQS ${INGEST_QUEUE}: ${QUEUE_URL}" >&2

# ----------------------------------------------------------------------------
# 4. EventBridge rule: CloudWatch ALARM state change → the ingest queue.
# ----------------------------------------------------------------------------
aws events put-rule \
  --name "${ALARM_RULE}" \
  --description "Route CloudWatch alarm state changes to the Relay ingest queue." \
  --event-pattern '{"source":["aws.cloudwatch"],"detail-type":["CloudWatch Alarm State Change"],"detail":{"state":{"value":["ALARM"]}}}' \
  >/dev/null
RULE_ARN="arn:aws:events:${AWS_REGION}:${ACCOUNT_ID}:rule/${ALARM_RULE}"
echo "  EventBridge rule ${ALARM_RULE}: ${RULE_ARN}" >&2

# Allow EventBridge to deliver to the queue (queue policy, scoped to this rule).
# Policy is a JSON string nested in --attributes, so pass the whole value as a
# JSON document (file://) rather than shorthand.
_POLICY_TMP="$(mktemp)"
trap 'rm -f "${_POLICY_TMP}"' EXIT
python3 -c "
import json, sys
queue_arn, rule_arn = sys.argv[1], sys.argv[2]
policy = {
  'Version': '2012-10-17',
  'Statement': [{
    'Sid': 'AllowEventBridgeToRelayIngest',
    'Effect': 'Allow',
    'Principal': {'Service': 'events.amazonaws.com'},
    'Action': 'sqs:SendMessage',
    'Resource': queue_arn,
    'Condition': {'ArnEquals': {'aws:SourceArn': rule_arn}},
  }],
}
print(json.dumps({'Policy': json.dumps(policy)}))
" "${QUEUE_ARN}" "${RULE_ARN}" > "${_POLICY_TMP}"
aws sqs set-queue-attributes --queue-url "${QUEUE_URL}" \
  --attributes "file://${_POLICY_TMP}" >/dev/null
rm -f "${_POLICY_TMP}"
trap - EXIT

# put-targets takes a list of JSON objects; pass it as a JSON document (file://).
_TARGETS_TMP="$(mktemp)"
trap 'rm -f "${_TARGETS_TMP}"' EXIT
python3 -c "
import json, sys
print(json.dumps([{'Id': 'relay-ingest', 'Arn': sys.argv[1]}]))
" "${QUEUE_ARN}" > "${_TARGETS_TMP}"
aws events put-targets --rule "${ALARM_RULE}" \
  --targets "file://${_TARGETS_TMP}" >/dev/null
rm -f "${_TARGETS_TMP}"
trap - EXIT

# ----------------------------------------------------------------------------
# Summary — the env vars to export before running Relay locally.
# ----------------------------------------------------------------------------
cat >&2 <<SUMMARY

Provisioned. Export these before running Relay locally (see docs/local-dev.md).
These mirror the env vars RelayComputeStack sets on the Fargate container:

  export RELAY_FLEET_TABLE_NAME=${TABLE}
  export RELAY_TABLE_NAME=${TABLE}
  export RELAY_SQS_QUEUE_URL=${QUEUE_URL}
  export RELAY_SNS_TOPIC_ARN=${PAGING_TOPIC_ARN}
  export RELAY_PAGING_TOPIC_ARN=${PAGING_TOPIC_ARN}
  export RELAY_CENTRAL_PAGING_TOPIC_ARN=${CENTRAL_TOPIC_ARN}
  export AWS_REGION=${AWS_REGION}

To tear these down later: scripts/relay-down.sh does NOT remove them (it only
scales ECS). Run scripts/relay-teardown-cli.sh (same RELAY_TEAM_NAME) for a
clean slate — it deletes these resources in dependency-safe order.
SUMMARY
