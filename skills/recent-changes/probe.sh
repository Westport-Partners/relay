#!/usr/bin/env bash
# recent-changes/probe.sh — READ-ONLY recent-change correlation.
#
# Uses only describe*/list*/get*/lookup-events calls. Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION          required  AWS region
#   RELAY_APP_NAME        required  app name (used to discover ECS cluster/service and CFN stack)
#   RELAY_ECS_CLUSTER     optional  cluster name/ARN (else discovered by app name)
#   RELAY_ECS_SERVICE     optional  service name (else discovered within cluster)
#   RELAY_CFN_STACK       optional  CloudFormation stack name (else matched by app name)
#   RELAY_WINDOW_MINUTES  optional  lookback window in minutes (default 1440 = 24 h; wider than
#                                   other probes because deploys often precede alarms by hours)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
APP="${RELAY_APP_NAME:-}"
CLUSTER="${RELAY_ECS_CLUSTER:-}"
SERVICE="${RELAY_ECS_SERVICE:-}"
CFN_STACK="${RELAY_CFN_STACK:-}"
WINDOW="${RELAY_WINDOW_MINUTES:-1440}"

if [ -z "${REGION}" ] || [ -z "${APP}" ]; then
  echo "ERROR: RELAY_REGION and RELAY_APP_NAME are required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# Compute UTC start time for the window (GNU date, Linux/WSL)
START_TIME="$(date -u -d "-${WINDOW} minutes" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || {
  note "Could not compute start time with GNU date; falling back to python3."
  START_TIME="$(python3 -c "
from datetime import datetime, timezone, timedelta
print((datetime.now(timezone.utc) - timedelta(minutes=${WINDOW})).strftime('%Y-%m-%dT%H:%M:%SZ'))
")"
}

# ---------------------------------------------------------------------------
# 1. Resolution — what the probe is investigating
# ---------------------------------------------------------------------------
section "Resolution"
echo "  App name  : ${APP}"
echo "  Region    : ${REGION}"
echo "  Window    : ${WINDOW} minutes (from ${START_TIME} UTC)"

# Resolve ECS cluster
if [ -z "${CLUSTER}" ]; then
  note "RELAY_ECS_CLUSTER not set; discovering by app name '${APP}'."
  mapfile -t CLUSTER_CANDIDATES < <("${AWS[@]}" ecs list-clusters \
      --query 'clusterArns[]' --output text 2>/dev/null | tr '\t' '\n')
  for c in "${CLUSTER_CANDIDATES[@]:-}"; do
    [ -z "${c}" ] && continue
    short="${c##*/}"
    if printf '%s' "${short}" | grep -qi "${APP}"; then CLUSTER="${c}"; break; fi
  done
  [ -z "${CLUSTER}" ] && [ "${#CLUSTER_CANDIDATES[@]}" -gt 0 ] && {
    CLUSTER="${CLUSTER_CANDIDATES[0]}"
    note "No cluster name-match for '${APP}'; using first cluster found."
  }
fi
[ -n "${CLUSTER}" ] && echo "  Cluster   : ${CLUSTER}" || note "No ECS cluster resolved; ECS sections will be skipped."

# Resolve ECS service (only if cluster found)
if [ -n "${CLUSTER}" ] && [ -z "${SERVICE}" ]; then
  note "RELAY_ECS_SERVICE not set; discovering by app name within cluster."
  mapfile -t SVC_CANDIDATES < <("${AWS[@]}" ecs list-services --cluster "${CLUSTER}" \
      --query 'serviceArns[]' --output text 2>/dev/null | tr '\t' '\n')
  for s in "${SVC_CANDIDATES[@]:-}"; do
    [ -z "${s}" ] && continue
    short="${s##*/}"
    if printf '%s' "${short}" | grep -qi "${APP}"; then SERVICE="${s}"; break; fi
  done
  [ -z "${SERVICE}" ] && [ "${#SVC_CANDIDATES[@]}" -gt 0 ] && {
    SERVICE="${SVC_CANDIDATES[0]}"
    note "No service name-match for '${APP}'; using first service found."
  }
fi
[ -n "${SERVICE}" ] && echo "  Service   : ${SERVICE}" || note "No ECS service resolved; ECS deployment section will be skipped."

# Resolve CloudFormation stack
if [ -z "${CFN_STACK}" ]; then
  note "RELAY_CFN_STACK not set; searching for stack matching '${APP}'."
  CFN_STACK="$("${AWS[@]}" cloudformation list-stacks \
      --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
                             ROLLBACK_COMPLETE DELETE_FAILED \
      --query 'StackSummaries[].StackName' --output text 2>/dev/null \
    | tr '\t' '\n' \
    | grep -i "${APP}" \
    | head -1)" || true
  [ -n "${CFN_STACK}" ] && echo "  CFN stack : ${CFN_STACK} (discovered)" \
    || note "No CloudFormation stack matched '${APP}'; CFN events section will note this."
fi
[ -n "${CFN_STACK}" ] && echo "  CFN stack : ${CFN_STACK}"

# ---------------------------------------------------------------------------
# 2. ECS deployments — recent task-def rollouts and rollout state
# ---------------------------------------------------------------------------
section "ECS deployments"
if [ -z "${CLUSTER}" ] || [ -z "${SERVICE}" ]; then
  note "Cluster or service not resolved; skipping ECS deployment section."
else
  ECS_DESC="$("${AWS[@]}" ecs describe-services --cluster "${CLUSTER}" \
      --services "${SERVICE}" 2>/dev/null)" || true
  if [ -z "${ECS_DESC}" ]; then
    note "describe-services returned nothing (permission denied or service not found)."
  else
    printf '%s' "${ECS_DESC}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
svcs = d.get('services', [])
if not svcs:
    print('  [note] no services in response.')
    sys.exit(0)
svc = svcs[0]
deps = svc.get('deployments', [])
if not deps:
    print('  [note] no deployment records found.')
    sys.exit(0)
for dep in deps:
    td = dep.get('taskDefinition', '').split('/')[-1]
    print('  - status=%-8s rolloutState=%-12s  taskDef=%-40s' % (
          dep.get('status', '?'),
          str(dep.get('rolloutState', '?')),
          td))
    print('    desired=%-3s running=%-3s pending=%-3s failedTasks=%-3s' % (
          dep.get('desiredCount', '?'), dep.get('runningCount', '?'),
          dep.get('pendingCount', '?'), dep.get('failedTasks', '?')))
    print('    createdAt=%s  updatedAt=%s' % (
          dep.get('createdAt', '?'), dep.get('updatedAt', '?')))
    if dep.get('rolloutStateReason'):
        print('    rolloutReason:', dep['rolloutStateReason'])
" 2>/dev/null || note "could not parse ECS deployments response."
  fi
fi

# ---------------------------------------------------------------------------
# 3. CloudFormation recent activity
# ---------------------------------------------------------------------------
section "CloudFormation recent activity"

# 3a. Matched stack events in the window
if [ -z "${CFN_STACK}" ]; then
  note "No stack resolved; skipping stack event detail."
else
  CFN_EVENTS="$("${AWS[@]}" cloudformation describe-stack-events \
      --stack-name "${CFN_STACK}" \
      --query "StackEvents[?Timestamp>='${START_TIME}']" 2>/dev/null)" || true
  if [ -z "${CFN_EVENTS}" ] || [ "${CFN_EVENTS}" = "[]" ]; then
    note "No stack events for '${CFN_STACK}' since ${START_TIME} (or describe-stack-events denied)."
  else
    printf '%s' "${CFN_EVENTS}" | python3 -c "
import sys, json
events = json.load(sys.stdin)
mutating = [e for e in events if any(
    e.get('ResourceStatus','').startswith(p)
    for p in ('UPDATE','CREATE','DELETE','ROLLBACK'))]
if not mutating:
    print('  [note] no UPDATE/CREATE/DELETE/ROLLBACK events in the window for this stack.')
    sys.exit(0)
print('  Stack events in window (mutating statuses only):')
for e in mutating:
    print('  - %s  %-40s %-30s %s' % (
          e.get('Timestamp','?'),
          e.get('ResourceStatus','?'),
          e.get('ResourceType','?'),
          e.get('LogicalResourceId','?')))
    if e.get('ResourceStatusReason'):
        print('    reason:', e['ResourceStatusReason'])
" 2>/dev/null || note "could not parse stack events."
  fi
fi

# 3b. Recently-updated stacks (broader view — catch sibling/shared stacks)
echo ""
note "Listing all stacks updated since ${START_TIME} (LastUpdatedTime):"
"${AWS[@]}" cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
                           ROLLBACK_COMPLETE DELETE_FAILED UPDATE_IN_PROGRESS \
    --query "StackSummaries[?LastUpdatedTime>='${START_TIME}']" 2>/dev/null \
  | python3 -c "
import sys, json
stacks = json.load(sys.stdin)
if not stacks:
    print('  [note] no stacks updated in the window.')
    sys.exit(0)
for s in stacks:
    print('  - %-50s %s  lastUpdated=%s' % (
          s.get('StackName','?'),
          s.get('StackStatus','?'),
          s.get('LastUpdatedTime','?')))
" 2>/dev/null || note "could not parse list-stacks response."

# ---------------------------------------------------------------------------
# 4. CloudTrail mutating events — "who changed what"
# ---------------------------------------------------------------------------
section "CloudTrail mutating events (last ${WINDOW}m)"
CT_RAW="$("${AWS[@]}" cloudtrail lookup-events \
    --start-time "${START_TIME}" \
    --query 'Events[]' 2>/dev/null)" || true

if [ -z "${CT_RAW}" ] || [ "${CT_RAW}" = "[]" ]; then
  note "No CloudTrail events returned (window too narrow, no changes, or cloudtrail:LookupEvents denied)."
else
  printf '%s' "${CT_RAW}" | python3 -c "
import sys, json

MUTATING_PREFIXES = (
    'Create', 'Update', 'Delete', 'Put', 'Modify',
    'Attach', 'Detach', 'Set', 'Run', 'Start', 'Stop',
    'Reboot', 'Terminate', 'Revoke', 'Authorize', 'Register',
    'Deregister', 'Associate', 'Disassociate', 'Enable', 'Disable',
)

events = json.load(sys.stdin)
mutating = [
    e for e in events
    if any(e.get('EventName', '').startswith(p) for p in MUTATING_PREFIXES)
]
if not mutating:
    print('  [note] no write/mutating events matched in the window.')
    sys.exit(0)

print('  %d mutating event(s) found:' % len(mutating))
for e in mutating:
    user = '?'
    try:
        ct = json.loads(e.get('CloudTrailEvent', '{}'))
        uid = ct.get('userIdentity', {})
        user = uid.get('userName') or uid.get('sessionContext', {}).get(
               'sessionIssuer', {}).get('userName') or uid.get('arn', '?')
    except Exception:
        pass
    resources = e.get('Resources') or []
    res_str = ', '.join(
        r.get('ResourceName', r.get('ResourceType', '?'))
        for r in resources
    ) if resources else '(no resource listed)'
    print('  - %s  %-45s user=%-25s resources=%s' % (
          e.get('EventTime', '?'),
          e.get('EventName', '?'),
          user,
          res_str))
" 2>/dev/null || note "could not parse CloudTrail events response."
fi

# ---------------------------------------------------------------------------
# 5. Note on GitLab deploy correlation
# ---------------------------------------------------------------------------
section "GitLab deploy correlation"
note "GitLab pipeline and MR data are not queried here."
note "Deploy/MR context is attached to the incident by the Relay Hub's deploy-context"
note "enrichment step (see docs/AI.md §4). Cross-reference the timestamps above"
note "against the Hub's deploy timeline for the full picture."

echo ""
echo "Done. All findings above are hypotheses — a change in the window is a"
echo "suspect, not a confirmed cause. Correlate timestamps with the alarm time"
echo "and pivot to the appropriate skill (ecs-investigation, network-connectivity,"
echo "iam-permissions, database-connectivity) based on the change type found."
