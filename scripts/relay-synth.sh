#!/usr/bin/env bash
# relay-synth.sh — synthesize the CloudFormation for the selected Relay topology.
# No AWS writes. Safe to run anywhere. Used by the pipeline's synth job and locally.
#
# Usage:  RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=westport ./scripts/relay-synth.sh
#         (RELAY_DEPLOY_TYPE: team | federated-hub; default team)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/relay-context.sh"

# cdk.json runs a bare `python3 infra/app.py`; without the venv on PATH that
# fails ModuleNotFoundError: aws_cdk (debug doc gotcha #3). Activate it if present
# — same as relay-deploy.sh so synth and deploy behave identically.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate"
fi

relay_resolve_account
relay_build_context

cd "${RELAY_ROOT}"
echo "Synthesizing: ${RELAY_STACKS}" >&2
# Synthesize to cdk.out (works for one OR many stacks; multi-stack synth does
# not echo a template to stdout, so we rely on the cdk.out artifacts).
# shellcheck disable=SC2086
relay_cdk synth ${RELAY_STACKS} ${RELAY_CDK_CONTEXT} >/dev/null
echo "Synthesized templates in ${RELAY_ROOT}/cdk.out:" >&2
ls -1 "${RELAY_ROOT}"/cdk.out/*.template.json >&2 2>/dev/null || true
