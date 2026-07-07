#!/usr/bin/env bash
# relay-deploy-direct.sh — deploy Relay WITHOUT cdk deploy's iam:PassRole step.
#
# WHY THIS EXISTS
# ---------------
# `cdk deploy` hands the CDK bootstrap CloudFormation execution role to
# CloudFormation via iam:PassRole. Locked-down accounts (FedRAMP, GovCloud,
# regulated enterprise / government) commonly DENY iam:PassRole in an
# identity-based policy, so `cdk deploy` fails outright with:
#
#   ...is not authorized to perform: iam:PassRole on resource:
#   .../cdk-hnb659fds-cfn-exec-role-... with an explicit deny ...
#
# This script splits the operation in two so no PassRole is ever needed:
#   1. cdk synth  — produces CloudFormation templates locally. No AWS writes.
#   2. aws cloudformation deploy — submits each template using the CALLER'S
#      OWN credentials. CloudFormation acts as the caller; there is no
#      separate execution role to pass.
#
# Relay's stacks reference the container image by registry URI (no CDK file or
# image assets), so the synthesized templates are self-contained and deploy
# cleanly this way. The DATA plane (RelayDataStack) creates zero IAM roles and
# zero VPC, so it deploys here even in the most restricted accounts. The COMPUTE
# stack works too, but supply BYOR/BYOV context (relay:ecs_task_role_arn,
# relay:ecs_execution_role_arn, relay:vpc_id) in accounts that also deny
# iam:CreateRole / ec2:CreateVpc — see docs/byor.md.
#
# Usage (same env-var surface as relay-deploy.sh):
#   RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=<team> RELAY_STACK_SELECTOR=data \
#     ./scripts/relay-deploy-direct.sh
#
#   RELAY_CFN_DEPLOY_EXTRA can pass extra flags to the CloudFormation call,
#   e.g. RELAY_CFN_DEPLOY_EXTRA="--role-arn <arn>" to use a service role.
#   (Valid on `deploy`, `create-stack`, and `update-stack`.)
#
#   RELAY_CFN_MODE selects the deploy engine (default: STANDARD):
#     STANDARD — `aws cloudformation deploy` (the high-level wrapper). Waits for
#                every resource to reach a ready-to-serve state before returning
#                — e.g. full ECS service stabilization (health checks pass).
#     EXPRESS  — raw `create-stack`/`update-stack` with
#                `--deployment-config '{"Mode":"EXPRESS",...}'`. Returns as soon
#                as resource *configuration* is applied; ECS/ALB keep coming up
#                in the background. Cuts a re-deploy from the full stabilization
#                wait (often 15-20+ min for the compute stack) to seconds.
#                Tradeoffs: (1) success no longer means "serving traffic" — poll
#                the service yourself if you need that gate; (2) an image-only
#                update to the SAME tag still won't roll (task def unchanged) —
#                bump the tag or force-new-deployment; (3) needs AWS CLI >= 2.35
#                (added `--deployment-config`). DisableRollback stays false so a
#                failed EXPRESS update rolls back instead of stranding the stack.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/relay-context.sh"

# cdk.json runs a bare `python3 infra/app.py`; activate the venv if present so
# the synth step finds aws_cdk (same as relay-synth.sh / relay-deploy.sh).
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate"
fi

relay_resolve_account
relay_build_context

cd "${RELAY_ROOT}"

# 1. Synthesize the selected stacks to cdk.out (no AWS writes).
echo "Synthesizing (no AWS writes): ${RELAY_STACKS}" >&2
# shellcheck disable=SC2086
relay_cdk synth ${RELAY_STACKS} ${RELAY_CDK_CONTEXT} "$@" >/dev/null

# Deploy mode: STANDARD (default) or EXPRESS. Normalize to upper-case so
# RELAY_CFN_MODE=express and =EXPRESS behave the same.
_cfn_mode="$(printf '%s' "${RELAY_CFN_MODE:-STANDARD}" | tr '[:lower:]' '[:upper:]')"
case "${_cfn_mode}" in
  STANDARD|EXPRESS) : ;;
  *) echo "ERROR: RELAY_CFN_MODE='${RELAY_CFN_MODE}' (expected STANDARD|EXPRESS)" >&2; exit 1 ;;
esac

# EXPRESS uses raw create-stack/update-stack (the high-level `deploy` wrapper does
# not expose --deployment-config). Fail fast if the CLI predates the feature so a
# stray flag can't be silently ignored.
if [ "${_cfn_mode}" = "EXPRESS" ]; then
  if ! aws cloudformation create-stack help 2>/dev/null | grep -q -- '--deployment-config'; then
    echo "ERROR: RELAY_CFN_MODE=EXPRESS needs AWS CLI >= 2.35 (--deployment-config)." >&2
    echo "       Installed: $(aws --version 2>&1). Update the CLI or unset RELAY_CFN_MODE." >&2
    exit 1
  fi
fi

# Deploy one synthesized template via raw create-stack/update-stack in EXPRESS
# mode. STANDARD callers never reach this. DisableRollback stays false so a
# failed EXPRESS update rolls back rather than stranding the stack (Issue 8).
_deploy_express() {
  local _stack="$1" _template="$2"
  # --template-body caps at 51,200 bytes (same limit the high-level `deploy`
  # wrapper enforces without an S3 bucket). Guard with a clear message instead
  # of a raw ValidationError, and point at the STANDARD fallback.
  local _size
  _size="$(wc -c < "${_template}" | tr -d ' ')"
  if [ "${_size}" -gt 51200 ]; then
    echo "ERROR: ${_stack} template is ${_size} bytes (> 51200) — too large for" >&2
    echo "       EXPRESS mode's inline --template-body. Deploy this stack with" >&2
    echo "       RELAY_CFN_MODE=STANDARD (uploads to S3 automatically)." >&2
    exit 1
  fi

  local _status
  _status="$(aws cloudformation describe-stacks \
    --stack-name "${_stack}" --region "${AWS_REGION}" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")"

  # A prior failed create leaves a ROLLBACK_COMPLETE stack that can only be
  # deleted, not updated — clear it so the create below succeeds.
  if [ "${_status}" = "ROLLBACK_COMPLETE" ]; then
    echo "  ${_stack} is ROLLBACK_COMPLETE — deleting before re-create..." >&2
    aws cloudformation delete-stack --stack-name "${_stack}" --region "${AWS_REGION}"
    aws cloudformation wait stack-delete-complete --stack-name "${_stack}" --region "${AWS_REGION}"
    _status="DOES_NOT_EXIST"
  fi

  if [ "${_status}" = "DOES_NOT_EXIST" ]; then
    echo "Deploying ${_stack} via create-stack (EXPRESS, no PassRole)..." >&2
    # shellcheck disable=SC2086
    aws cloudformation create-stack \
      --stack-name "${_stack}" \
      --template-body "file://${_template}" \
      --capabilities CAPABILITY_NAMED_IAM \
      --deployment-config '{"Mode":"EXPRESS","DisableRollback":false}' \
      --region "${AWS_REGION}" \
      ${RELAY_CFN_DEPLOY_EXTRA:-}
    aws cloudformation wait stack-create-complete \
      --stack-name "${_stack}" --region "${AWS_REGION}"
  else
    echo "Deploying ${_stack} via update-stack (EXPRESS, no PassRole)..." >&2
    # update-stack errors with "No updates are to be performed" on an empty
    # changeset; treat that as success (parity with --no-fail-on-empty-changeset).
    local _err
    # shellcheck disable=SC2086
    if ! _err="$(aws cloudformation update-stack \
        --stack-name "${_stack}" \
        --template-body "file://${_template}" \
        --capabilities CAPABILITY_NAMED_IAM \
        --deployment-config '{"Mode":"EXPRESS","DisableRollback":false}' \
        --region "${AWS_REGION}" \
        ${RELAY_CFN_DEPLOY_EXTRA:-} 2>&1)"; then
      if printf '%s' "${_err}" | grep -q "No updates are to be performed"; then
        echo "  ${_stack}: no changes — skipping." >&2
        return 0
      fi
      echo "${_err}" >&2
      return 1
    fi
    aws cloudformation wait stack-update-complete \
      --stack-name "${_stack}" --region "${AWS_REGION}"
  fi
}

# 2. Deploy each synthesized template directly via CloudFormation, using the
#    caller's credentials. No PassRole, no bootstrap execution role.
echo "Deploy mode: ${_cfn_mode}" >&2
for _stack in ${RELAY_STACKS}; do
  _template="${RELAY_ROOT}/cdk.out/${_stack}.template.json"
  if [ ! -f "${_template}" ]; then
    echo "ERROR: expected template not found: ${_template}" >&2
    echo "       (synth may have skipped ${_stack}; check the context above)" >&2
    exit 1
  fi
  if [ "${_cfn_mode}" = "EXPRESS" ]; then
    _deploy_express "${_stack}" "${_template}"
  else
    echo "Deploying ${_stack} via aws cloudformation deploy (no PassRole)..." >&2
    # shellcheck disable=SC2086
    aws cloudformation deploy \
      --template-file "${_template}" \
      --stack-name "${_stack}" \
      --capabilities CAPABILITY_NAMED_IAM \
      --no-fail-on-empty-changeset \
      --region "${AWS_REGION}" \
      ${RELAY_CFN_DEPLOY_EXTRA:-}
  fi
done

echo "Direct deploy complete: ${RELAY_STACKS}" >&2
echo "Outputs (per stack):" >&2
for _stack in ${RELAY_STACKS}; do
  aws cloudformation describe-stacks \
    --stack-name "${_stack}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs" --output table 2>/dev/null || true
done
