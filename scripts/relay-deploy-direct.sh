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
#   RELAY_CFN_DEPLOY_EXTRA can pass extra flags to `aws cloudformation deploy`,
#   e.g. RELAY_CFN_DEPLOY_EXTRA="--role-arn <arn>" to use a service role.
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
relay_cdk synth ${RELAY_STACKS} ${RELAY_CDK_CONTEXT} >/dev/null

# 2. Deploy each synthesized template directly via CloudFormation, using the
#    caller's credentials. No PassRole, no bootstrap execution role.
for _stack in ${RELAY_STACKS}; do
  _template="${RELAY_ROOT}/cdk.out/${_stack}.template.json"
  if [ ! -f "${_template}" ]; then
    echo "ERROR: expected template not found: ${_template}" >&2
    echo "       (synth may have skipped ${_stack}; check the context above)" >&2
    exit 1
  fi
  echo "Deploying ${_stack} via aws cloudformation deploy (no PassRole)..." >&2
  # shellcheck disable=SC2086
  aws cloudformation deploy \
    --template-file "${_template}" \
    --stack-name "${_stack}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --region "${AWS_REGION}" \
    ${RELAY_CFN_DEPLOY_EXTRA:-}
done

echo "Direct deploy complete: ${RELAY_STACKS}" >&2
echo "Outputs (per stack):" >&2
for _stack in ${RELAY_STACKS}; do
  aws cloudformation describe-stacks \
    --stack-name "${_stack}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs" --output table 2>/dev/null || true
done
