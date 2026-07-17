#!/usr/bin/env bash
# relay-synth-release-templates.sh — pre-synthesize CFN templates + BYOR policy
# docs as release/review artifacts (issues #112, #113).
#
# WHY THIS EXISTS
# ----------------
# #112: publish the CloudFormation templates a tag would deploy, so a reviewer
# can read them before any AWS credentials touch the account. #113: publish the
# exact BYOR inline-policy/trust JSON from a representative synth, so a security
# team can pre-approve the permission shape before a first BYOR deploy — the
# chicken-and-egg problem where you can't get the policy without deploying and
# can't deploy without pre-approval (see docs/byor.md).
#
# Zero AWS credentials required: no `aws sts get-caller-identity` (we source
# relay-context.sh only for its relay_cdk() wrapper, never relay_resolve_account
# or relay_build_context), and BYOV is never exercised (no relay:vpc_id set),
# so the compute stack's Vpc.from_lookup — the one call in this whole path that
# would hit a live AWS API — never fires.
#
# Usage: ./scripts/relay-synth-release-templates.sh <version>   (e.g. 1.2.3, no leading v)
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "ERROR: usage: $0 <version>  (e.g. 1.2.3, no leading 'v')" >&2
  exit 1
fi
VERSION="$1"

# Source relay-context.sh ONLY for the relay_cdk() helper — do NOT call
# relay_resolve_account / relay_build_context, both of which shell out to
# `aws sts get-caller-identity` and require live AWS credentials this script
# must not depend on.
source "$(dirname "${BASH_SOURCE[0]}")/relay-context.sh"

# cdk.json runs a bare `python3 infra/app.py`; activate the venv if present so
# it finds aws_cdk (same as relay-synth.sh / relay-deploy-direct.sh).
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/activate"
fi

cd "${RELAY_ROOT}"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required but not found on PATH." >&2
  exit 1
fi

IMAGE_URI="ghcr.io/westport-partners/relay:${VERSION}"

# Common context shared by both variants. relay:aws_account is deliberately
# left unset so the template stays account-agnostic (${AWS::AccountId}
# pseudo-parameter) — this is a portable release artifact, not a live deploy.
# relay:aws_region IS set: the BYOR output-emission code
# (compute_stack.py:_emit_byor_outputs) reads it directly (not self.region) to
# build literal, pasteable ARNs — without it the byor variant's outputs would
# embed an unresolved token instead of "us-east-1".
COMMON_CTX=(
  -c "relay:role=team"
  -c "relay:team_name=example-team"
  -c "relay:aws_region=us-east-1"
  -c "relay:hub_image_uri=${IMAGE_URI}"
)

DEFAULT_OUT="${RELAY_ROOT}/cdk.out/release-default"
BYOR_OUT="${RELAY_ROOT}/cdk.out/release-byor"
ARTIFACTS_DIR="${RELAY_ROOT}/cdk.out/release-artifacts"

echo "Synthesizing default-variant templates (version ${VERSION})..." >&2
relay_cdk synth RelayDataStack RelayComputeStack \
  --app "python3 infra/app.py" \
  -o "${DEFAULT_OUT}" \
  "${COMMON_CTX[@]}" \
  >/dev/null

echo "Synthesizing BYOR-variant templates (version ${VERSION})..." >&2
relay_cdk synth RelayDataStack RelayComputeStack \
  --app "python3 infra/app.py" \
  -o "${BYOR_OUT}" \
  "${COMMON_CTX[@]}" \
  -c "relay:ecs_task_role_arn=arn:aws:iam::123456789012:role/relay-ecs-task" \
  -c "relay:ecs_execution_role_arn=arn:aws:iam::123456789012:role/relay-ecs-execution" \
  >/dev/null

mkdir -p "${ARTIFACTS_DIR}"

# The data stack is unaffected by BYOR (BYOR only touches the compute stack),
# so it's emitted once from the default variant.
cp "${DEFAULT_OUT}/RelayDataStack.template.json" \
  "${ARTIFACTS_DIR}/relay-cfn-${VERSION}-data.json"
cp "${DEFAULT_OUT}/RelayComputeStack.template.json" \
  "${ARTIFACTS_DIR}/relay-cfn-${VERSION}-compute.json"
cp "${BYOR_OUT}/RelayComputeStack.template.json" \
  "${ARTIFACTS_DIR}/relay-cfn-${VERSION}-compute-byor.json"

# Bundle the 3 BYOR policy outputs as parsed JSON (not double-encoded strings)
# into one file — a stable, versioned artifact for change-management review
# (docs/byor.md "Example policy documents").
jq -n \
  --slurpfile tmpl "${BYOR_OUT}/RelayComputeStack.template.json" \
  '{
    TaskRoleInlinePolicy: ($tmpl[0].Outputs.ByorTaskRoleInlinePolicy.Value | fromjson),
    ExecutionRoleInlinePolicy: ($tmpl[0].Outputs.ByorExecutionRoleInlinePolicy.Value | fromjson),
    EcsRoleTrust: ($tmpl[0].Outputs.ByorEcsRoleTrust.Value | fromjson)
  }' > "${ARTIFACTS_DIR}/relay-byor-inline-policies-${VERSION}.json"

echo "Release artifacts in ${ARTIFACTS_DIR}:" >&2
ls -1 "${ARTIFACTS_DIR}"/*"${VERSION}"* >&2
