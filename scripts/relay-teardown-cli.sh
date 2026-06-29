#!/usr/bin/env bash
# relay-teardown-cli.sh — remove the data-plane resources created by
# relay-provision-cli.sh. The inverse of that script.
#
# WHY THIS EXISTS
# ---------------
# relay-provision-cli.sh stands up Relay's stateful AWS resources with plain
# `aws` calls so the product can be evaluated without CDK/CloudFormation.
# relay-down.sh only scales ECS to zero — it deliberately leaves these
# resources in place. This script is the documented "clean slate" path for an
# operator who provisioned via the CLI and now wants to start over or remove a
# test install.
#
# Deletes (in dependency-safe order):
#   EventBridge  relay-cloudwatch-alarm     remove target, then delete the rule
#   SQS          relay-hub-ingest           ingest queue
#   SQS          relay-hub-ingest-dlq       poison-message DLQ
#   SNS          relay-<team>-paging        team paging topic
#   SNS          relay-<team>-central-paging central paging topic
#   DynamoDB     relay-<team>               LAST — this is durable data
#
# DynamoDB deletion is guarded: it holds incident/contact/schedule data, so the
# script prompts for confirmation unless RELAY_FORCE=1 is set. Everything else
# is reconstructable by re-running relay-provision-cli.sh.
#
# Usage:
#   RELAY_TEAM_NAME=<team> [AWS_REGION=us-east-1] [RELAY_FORCE=1] \
#     ./scripts/relay-teardown-cli.sh
set -euo pipefail

RELAY_TEAM_NAME="${RELAY_TEAM_NAME:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="${AWS_REGION}"

if [ -z "${RELAY_TEAM_NAME}" ]; then
  echo "ERROR: RELAY_TEAM_NAME is required (identifies the relay-<team> table)." >&2
  exit 1
fi

TABLE="relay-${RELAY_TEAM_NAME}"
PAGING_TOPIC="relay-${RELAY_TEAM_NAME}-paging"
CENTRAL_TOPIC="relay-${RELAY_TEAM_NAME}-central-paging"
INGEST_QUEUE="relay-hub-ingest"
INGEST_DLQ="relay-hub-ingest-dlq"
ALARM_RULE="relay-cloudwatch-alarm"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "=== Relay CLI teardown ===" >&2
echo "Account: ${ACCOUNT_ID} / region: ${AWS_REGION}" >&2
echo "Target table: ${TABLE}" >&2

# ----------------------------------------------------------------------------
# 1. EventBridge — remove the target first, then the rule (a rule with targets
#    cannot be deleted without --force; removing the target keeps it explicit).
# ----------------------------------------------------------------------------
if aws events describe-rule --name "${ALARM_RULE}" >/dev/null 2>&1; then
  # Remove ALL targets by listing them dynamically. The CLI path registers
  # 'relay-ingest'; a CDK RelayComputeStack against the same rule registers
  # 'relay-ingest-sqs'. A hardcoded id would orphan the other and then
  # delete-rule fails ("Rule can't be deleted since it has targets").
  _target_ids="$(aws events list-targets-by-rule --rule "${ALARM_RULE}" \
    --query 'Targets[].Id' --output text 2>/dev/null | tr '\t' ' ')"
  if [ -n "${_target_ids}" ]; then
    # shellcheck disable=SC2086
    aws events remove-targets --rule "${ALARM_RULE}" --ids ${_target_ids} >/dev/null 2>&1 || true
  fi
  aws events delete-rule --name "${ALARM_RULE}" >/dev/null
  echo "  EventBridge ${ALARM_RULE}: deleted" >&2
else
  echo "  EventBridge ${ALARM_RULE}: not found (skipping)" >&2
fi

# ----------------------------------------------------------------------------
# 2. SQS — delete the ingest queue, then the DLQ. (Order is not strictly
#    required once the EventBridge target is gone, but ingest-before-DLQ keeps
#    it tidy.)
# ----------------------------------------------------------------------------
for _q in "${INGEST_QUEUE}" "${INGEST_DLQ}"; do
  _url="$(aws sqs get-queue-url --queue-name "${_q}" --query QueueUrl --output text 2>/dev/null || true)"
  if [ -n "${_url}" ] && [ "${_url}" != "None" ]; then
    aws sqs delete-queue --queue-url "${_url}" >/dev/null
    echo "  SQS ${_q}: deleted" >&2
  else
    echo "  SQS ${_q}: not found (skipping)" >&2
  fi
done

# ----------------------------------------------------------------------------
# 3. SNS — delete both paging topics. delete-topic is idempotent (no error if
#    the ARN is already gone), so resolve by name and delete when present.
# ----------------------------------------------------------------------------
for _t in "${PAGING_TOPIC}" "${CENTRAL_TOPIC}"; do
  _arn="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:${_t}"
  if aws sns get-topic-attributes --topic-arn "${_arn}" >/dev/null 2>&1; then
    aws sns delete-topic --topic-arn "${_arn}" >/dev/null
    echo "  SNS ${_t}: deleted" >&2
  else
    echo "  SNS ${_t}: not found (skipping)" >&2
  fi
done

# ----------------------------------------------------------------------------
# 4. DynamoDB — LAST, and guarded. This is the only durable, non-reconstructable
#    resource (incident/contact/schedule history). Confirm unless RELAY_FORCE=1.
# ----------------------------------------------------------------------------
if aws dynamodb describe-table --table-name "${TABLE}" >/dev/null 2>&1; then
  if [ "${RELAY_FORCE:-0}" != "1" ]; then
    echo "" >&2
    echo "About to DELETE DynamoDB table ${TABLE} — this destroys all incident," >&2
    echo "contact, and schedule data and cannot be undone." >&2
    printf "Type the table name to confirm: " >&2
    read -r _confirm
    if [ "${_confirm}" != "${TABLE}" ]; then
      echo "  DynamoDB ${TABLE}: confirmation did not match — left intact." >&2
      echo "  (Re-run with RELAY_FORCE=1 to skip the prompt, or delete by hand.)" >&2
      exit 0
    fi
  fi
  aws dynamodb delete-table --table-name "${TABLE}" >/dev/null
  echo "  DynamoDB ${TABLE}: delete requested (table will drain shortly)" >&2
else
  echo "  DynamoDB ${TABLE}: not found (skipping)" >&2
fi

echo "" >&2
echo "Teardown complete. Re-provision any time with scripts/relay-provision-cli.sh." >&2
