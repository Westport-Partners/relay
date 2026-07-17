#!/usr/bin/env bash
# relay-apply-byor-policies.sh — apply the BYOR inline policies + trust
# relationship to pre-provisioned ECS task/execution roles.
#
# WHY THIS EXISTS
# ----------------
# Step 3 of the deploy-byor.md runbook ("Account administrator applies the
# policies") is normally done by hand in the IAM console: paste the
# ByorTaskRoleInlinePolicy inline policy onto the task role, paste
# ByorExecutionRoleInlinePolicy onto the execution role, then merge
# ecs-tasks.amazonaws.com into both roles' trust policies. Individually these
# are easy to fat-finger, and it is the single most common place operators get
# stuck on a first BYOR deploy (SUGGEST-2). This script automates all of it.
#
# The three policy documents are read from the synthesized
# cdk.out/RelayComputeStack.template.json (same source as prompts/deploy-byor.md
# Step 2's `cat cdk.out/... | jq .Outputs`), falling back to the deployed
# stack's live CloudFormation Outputs if no local template is found. The
# template is the primary source deliberately: on a genuine first-time BYOR
# deploy, the ECS task can't start without the very policies this script
# applies, so the deploy's ECS circuit breaker trips and the stack lands in
# ROLLBACK_COMPLETE with Outputs=null — describe-stacks alone would leave this
# script unable to bootstrap the first deploy it exists to unblock. Run
# `relay-synth.sh` (or a prior `relay-deploy-direct.sh` attempt, successful or
# not) with the BYOR context flags first so cdk.out/ is populated. See
# prompts/deploy-byor.md Step 2.
#
# TRUST POLICY SAFETY
# --------------------
# Overwriting a role's AssumeRolePolicyDocument outright is destructive if the
# role is shared with anything else — it would silently drop any other
# trusted principal. This script never does that blindly:
#   - If ecs-tasks.amazonaws.com is already trusted, it's a no-op (idempotent).
#   - If not, it merges the required trust statement into the EXISTING
#     document (appending a statement, or extending an existing
#     Service-principal statement's Principal.Service list) and applies the
#     merged result.
#   - If the merge can't be done unambiguously and safely (unexpected trust
#     document shape), it prints the computed merged JSON and asks the
#     operator to confirm/apply manually rather than guessing.
#
# Usage:
#   ./scripts/relay-apply-byor-policies.sh <task-role-name> <exec-role-name> [--force-redeploy]
#
# Environment variables (all optional):
#   AWS_REGION / AWS_DEFAULT_REGION  — target region (default: us-east-1)
#   RELAY_FORCE_REDEPLOY=1           — same as passing --force-redeploy
#
# Requires: aws CLI, jq (same assumption as scripts/relay-preflight.sh).
#
# Exit codes:
#   0  — policies applied (or already up to date) and, if requested, redeploy triggered
#   1  — a required input was missing, an AWS call failed, or a merge could
#        not be applied safely without operator confirmation
set -euo pipefail

STACK_NAME="RelayComputeStack"
CLUSTER_NAME="relay-hub"
SERVICE_NAME="relay-hub"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELAY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_FILE="${RELAY_ROOT}/cdk.out/${STACK_NAME}.template.json"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
_force_redeploy="${RELAY_FORCE_REDEPLOY:-0}"
_positional=()
for _arg in "$@"; do
  case "${_arg}" in
    --force-redeploy) _force_redeploy=1 ;;
    -h|--help)
      sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | grep '^#' | sed 's/^# \?//'
      exit 0
      ;;
    -*) echo "ERROR: unknown flag '${_arg}'" >&2; exit 1 ;;
    *) _positional+=("${_arg}") ;;
  esac
done

if [ "${#_positional[@]}" -ne 2 ]; then
  echo "ERROR: expected exactly 2 positional args: <task-role-name> <exec-role-name>" >&2
  echo "Usage: $0 <task-role-name> <exec-role-name> [--force-redeploy]" >&2
  exit 1
fi

TASK_ROLE_NAME="${_positional[0]}"
EXEC_ROLE_NAME="${_positional[1]}"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required but not found on PATH." >&2
  exit 1
fi

echo "=== Relay BYOR policy apply ===" >&2
echo "Region:          ${AWS_REGION}" >&2
echo "Stack:           ${STACK_NAME}" >&2
echo "Task role:       ${TASK_ROLE_NAME}" >&2
echo "Execution role:  ${EXEC_ROLE_NAME}" >&2

# ---------------------------------------------------------------------------
# 1. Read the three policy documents — prefer the synthesized template (works
#    even when the stack itself is ROLLBACK_COMPLETE / has no live outputs),
#    falling back to the deployed stack's CloudFormation outputs.
# ---------------------------------------------------------------------------
echo "" >&2

if [ -f "${TEMPLATE_FILE}" ]; then
  echo "--- Reading policy outputs from ${TEMPLATE_FILE} ---" >&2
  _outputs_json="$(jq '.Outputs | to_entries | map({OutputKey: .key, OutputValue: .value.Value})' \
        "${TEMPLATE_FILE}" 2>&1)" || {
    echo "ERROR: could not parse ${TEMPLATE_FILE} as JSON:" >&2
    echo "  ${_outputs_json}" >&2
    exit 1
  }
else
  echo "--- No local template at ${TEMPLATE_FILE} — reading live outputs from ${STACK_NAME} ---" >&2
  _outputs_json="$(aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --region "${AWS_REGION}" \
        --query 'Stacks[0].Outputs' \
        --output json 2>&1)" || {
    echo "ERROR: could not describe stack '${STACK_NAME}' in ${AWS_REGION}:" >&2
    echo "  ${_outputs_json}" >&2
    echo "  Synth the compute stack first with the BYOR context flags (see prompts/deploy-byor.md Step 2)." >&2
    exit 1
  }
fi

_get_output() {
  local _key="$1"
  jq -r --arg k "${_key}" '.[] | select(.OutputKey == $k) | .OutputValue' <<<"${_outputs_json}"
}

TASK_POLICY_JSON="$(_get_output ByorTaskRoleInlinePolicy)"
EXEC_POLICY_JSON="$(_get_output ByorExecutionRoleInlinePolicy)"
ECS_TRUST_JSON="$(_get_output ByorEcsRoleTrust)"

for _pair in "ByorTaskRoleInlinePolicy:${TASK_POLICY_JSON}" \
             "ByorExecutionRoleInlinePolicy:${EXEC_POLICY_JSON}" \
             "ByorEcsRoleTrust:${ECS_TRUST_JSON}"; do
  _name="${_pair%%:*}"
  _val="${_pair#*:}"
  if [ -z "${_val}" ]; then
    echo "ERROR: output '${_name}' not found (or empty) on stack '${STACK_NAME}'." >&2
    echo "  Re-synth with the BYOR context flags (relay:ecs_task_role_arn / relay:ecs_execution_role_arn)" >&2
    echo "  so the compute stack emits the BYOR outputs. See prompts/deploy-byor.md Step 2." >&2
    exit 1
  fi
done
echo "Found all 3 policy outputs." >&2

# ---------------------------------------------------------------------------
# 2. Apply the inline policies (put-role-policy is idempotent: it overwrites
#    the named policy version, so re-running with the same stack outputs is
#    always safe and a no-op in effect).
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Applying inline policies ---" >&2

aws iam put-role-policy \
  --role-name "${TASK_ROLE_NAME}" \
  --policy-name ByorTaskRoleInlinePolicy \
  --policy-document "${TASK_POLICY_JSON}" \
  --region "${AWS_REGION}"
echo "  ${TASK_ROLE_NAME}: ByorTaskRoleInlinePolicy applied" >&2

aws iam put-role-policy \
  --role-name "${EXEC_ROLE_NAME}" \
  --policy-name ByorExecutionRoleInlinePolicy \
  --policy-document "${EXEC_POLICY_JSON}" \
  --region "${AWS_REGION}"
echo "  ${EXEC_ROLE_NAME}: ByorExecutionRoleInlinePolicy applied" >&2

# ---------------------------------------------------------------------------
# 3. Trust policy: merge ecs-tasks.amazonaws.com in, never blind-overwrite.
# ---------------------------------------------------------------------------
# Merge strategy, applied to a role's existing AssumeRolePolicyDocument:
#   - If a statement already trusts ecs-tasks.amazonaws.com (as a bare string
#     Principal.Service, or inside a Principal.Service array) -> no-op.
#   - Else if there's exactly one statement and its Principal is a Service
#     principal (string or array) with Effect=Allow and
#     Action=sts:AssumeRole -> extend that statement's Principal.Service to
#     include ecs-tasks.amazonaws.com (converting a bare string to a list if
#     needed), preserving any Condition already present.
#   - Else (empty/default doc, or a shape we don't recognize) -> append the
#     ByorEcsRoleTrust statement as a new Statement entry.
#   - If the document is genuinely ambiguous in a way that could silently
#     drop existing access (i.e. jq itself fails, or the doc isn't valid
#     JSON), print the computed document and ask the operator to apply it
#     by hand rather than guessing.
_merge_trust() {
  local _role_name="$1"
  local _current_doc _new_statement _merged _already_trusted

  echo "" >&2
  echo "--- Trust policy: ${_role_name} ---" >&2

  _current_doc="$(aws iam get-role \
        --role-name "${_role_name}" \
        --region "${AWS_REGION}" \
        --query 'Role.AssumeRolePolicyDocument' \
        --output json 2>&1)" || {
    echo "ERROR: could not read trust policy for role '${_role_name}':" >&2
    echo "  ${_current_doc}" >&2
    return 1
  }

  # aws iam get-role url-decodes and returns the doc as JSON already.
  if ! echo "${_current_doc}" | jq -e . >/dev/null 2>&1; then
    echo "ERROR: role '${_role_name}' trust policy is not valid JSON — refusing to guess a merge." >&2
    echo "Current value: ${_current_doc}" >&2
    return 1
  fi

  _already_trusted="$(echo "${_current_doc}" | jq -r '
    [.Statement[]?
     | select(.Effect == "Allow")
     | .Principal.Service
     | if type == "array" then .[] else . end]
    | any(. == "ecs-tasks.amazonaws.com")
  ')"

  if [ "${_already_trusted}" = "true" ]; then
    echo "  ${_role_name}: ecs-tasks.amazonaws.com already trusted — no change needed" >&2
    return 0
  fi

  _new_statement="$(echo "${ECS_TRUST_JSON}" | jq -c '.Statement[0]')"

  # Merge strategy: ALWAYS append the required statement as its own new
  # Statement entry (or as the sole statement if the document has none yet).
  # We deliberately do NOT try to fold ecs-tasks.amazonaws.com into an
  # existing statement's Principal.Service list — doing so could silently
  # drop that statement's own Condition block (e.g. an existing
  # aws:SourceAccount / aws:SourceArn scoping) by attaching an unrelated
  # trust to it. Appending a whole new statement can only ADD trust, never
  # remove or reshape an existing one, so it's always safe regardless of
  # what the current document's shape is.
  _merged="$(jq -n \
    --argjson current "${_current_doc}" \
    --argjson newstmt "${_new_statement}" \
    '
    ($current.Statement // []) as $stmts
    | $current
    | .Statement = ($stmts + [$newstmt])
    ' 2>&1)" || {
    echo "ERROR: jq failed while computing the merged trust policy for '${_role_name}':" >&2
    echo "  ${_merged}" >&2
    echo "Current trust policy (apply the merge manually):" >&2
    echo "${_current_doc}" | jq .
    return 1
  }

  if ! echo "${_merged}" | jq -e . >/dev/null 2>&1; then
    echo "WARN: could not confidently compute a safe merge for '${_role_name}'." >&2
    echo "Current trust policy:" >&2
    echo "${_current_doc}" | jq .
    echo "Required trust statement (merge in by hand, do not overwrite):" >&2
    echo "${_new_statement}" | jq .
    return 1
  fi

  echo "  ${_role_name}: merging ecs-tasks.amazonaws.com into trust policy" >&2
  aws iam update-assume-role-policy \
    --role-name "${_role_name}" \
    --policy-document "${_merged}" \
    --region "${AWS_REGION}"
  echo "  ${_role_name}: trust policy updated" >&2
}

_trust_status=0
_merge_trust "${TASK_ROLE_NAME}" || _trust_status=1
_merge_trust "${EXEC_ROLE_NAME}" || _trust_status=1

if [ "${_trust_status}" -ne 0 ]; then
  echo "" >&2
  echo "ERROR: one or more trust policy merges could not be applied safely." >&2
  echo "Review the printed JSON above and apply the merge manually in the IAM console," >&2
  echo "then re-run this script (inline policies above were already applied)." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Optional: force a fresh ECS deployment so the roles' new permissions
#    take effect on running tasks immediately.
# ---------------------------------------------------------------------------
if [ "${_force_redeploy}" = "1" ]; then
  echo "" >&2
  echo "--- Forcing new ECS deployment (${CLUSTER_NAME}/${SERVICE_NAME}) ---" >&2
  aws ecs update-service \
    --cluster "${CLUSTER_NAME}" \
    --service "${SERVICE_NAME}" \
    --force-new-deployment \
    --region "${AWS_REGION}" >/dev/null
  echo "  Force-new-deployment requested. Poll with:" >&2
  echo "    aws ecs wait services-stable --cluster ${CLUSTER_NAME} --services ${SERVICE_NAME} --region ${AWS_REGION}" >&2
else
  echo "" >&2
  echo "Redeploy NOT triggered (pass --force-redeploy or set RELAY_FORCE_REDEPLOY=1 to roll running tasks now)." >&2
fi

echo "" >&2
echo "Done." >&2
