#!/usr/bin/env bash
# ecs-investigation/probe.sh — READ-ONLY ECS service diagnostics.
#
# Uses only describe*/list* calls. Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION          required  AWS region
#   RELAY_APP_NAME        required  app name (used to discover cluster/service)
#   RELAY_ECS_CLUSTER     optional  cluster name/ARN (else discovered)
#   RELAY_ECS_SERVICE     optional  service name (else discovered)
#   RELAY_WINDOW_MINUTES  optional  lookback for stopped tasks (default 60)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
APP="${RELAY_APP_NAME:-}"
CLUSTER="${RELAY_ECS_CLUSTER:-}"
SERVICE="${RELAY_ECS_SERVICE:-}"
WINDOW="${RELAY_WINDOW_MINUTES:-60}"

if [ -z "${REGION}" ] || [ -z "${APP}" ]; then
  echo "ERROR: RELAY_REGION and RELAY_APP_NAME are required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Resolve cluster + service
# ---------------------------------------------------------------------------
section "Resolution"
if [ -z "${CLUSTER}" ]; then
  note "RELAY_ECS_CLUSTER not set; discovering by app name '${APP}'."
  mapfile -t CANDIDATES < <("${AWS[@]}" ecs list-clusters \
      --query 'clusterArns[]' --output text 2>/dev/null | tr '\t' '\n')
  for c in "${CANDIDATES[@]}"; do
    short="${c##*/}"
    if printf '%s' "${short}" | grep -qi "${APP}"; then CLUSTER="${c}"; break; fi
  done
  [ -z "${CLUSTER}" ] && [ "${#CANDIDATES[@]}" -gt 0 ] && CLUSTER="${CANDIDATES[0]}"
fi
if [ -z "${CLUSTER}" ]; then
  note "No ECS cluster found; the app may not run on ECS. Stopping."
  exit 0
fi
echo "  Cluster: ${CLUSTER}"

if [ -z "${SERVICE}" ]; then
  note "RELAY_ECS_SERVICE not set; discovering by app name within the cluster."
  mapfile -t SVCS < <("${AWS[@]}" ecs list-services --cluster "${CLUSTER}" \
      --query 'serviceArns[]' --output text 2>/dev/null | tr '\t' '\n')
  for s in "${SVCS[@]}"; do
    short="${s##*/}"
    if printf '%s' "${short}" | grep -qi "${APP}"; then SERVICE="${s}"; break; fi
  done
  [ -z "${SERVICE}" ] && [ "${#SVCS[@]}" -gt 0 ] && { SERVICE="${SVCS[0]}"; note "No name match; using first service."; }
fi
[ -z "${SERVICE}" ] && { note "No service found in cluster."; exit 0; }
echo "  Service: ${SERVICE}"

DESC="$("${AWS[@]}" ecs describe-services --cluster "${CLUSTER}" \
    --services "${SERVICE}" 2>/dev/null)"
[ -z "${DESC}" ] && { note "describe-services returned nothing (permission?)."; exit 0; }

jqf() { printf '%s' "${DESC}" | python3 -c "import sys,json;d=json.load(sys.stdin);$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
# 2. Service summary
# ---------------------------------------------------------------------------
section "Service summary"
jqf "
s=d['services'][0]
print('  desired   :', s.get('desiredCount'))
print('  running   :', s.get('runningCount'))
print('  pending   :', s.get('pendingCount'))
print('  launchType:', s.get('launchType') or (s.get('capacityProviderStrategy') and 'capacity-provider') or '?')
print('  taskDef   :', s.get('taskDefinition','').split('/')[-1])
print('  status    :', s.get('status'))
" || note "could not parse service summary."

# ---------------------------------------------------------------------------
# 3. Deployments / rollout state
# ---------------------------------------------------------------------------
section "Deployments"
jqf "
for dep in d['services'][0].get('deployments',[]):
    print('  -', dep.get('status'), 'rollout=' + str(dep.get('rolloutState')),
          'desired=%s running=%s pending=%s failed=%s' % (
            dep.get('desiredCount'), dep.get('runningCount'),
            dep.get('pendingCount'), dep.get('failedTasks')),
          'taskDef=' + dep.get('taskDefinition','').split('/')[-1])
    if dep.get('rolloutStateReason'): print('      reason:', dep['rolloutStateReason'])
" || note "could not parse deployments."

# ---------------------------------------------------------------------------
# 4. Recent service events
# ---------------------------------------------------------------------------
section "Service events (most recent first)"
jqf "
for e in d['services'][0].get('events',[])[:8]:
    print('  -', e.get('createdAt'), e.get('message'))
" || note "could not parse service events."

# ---------------------------------------------------------------------------
# 5. Recently stopped tasks + reasons
# ---------------------------------------------------------------------------
section "Stopped tasks (last ${WINDOW}m window)"
STOPPED="$("${AWS[@]}" ecs list-tasks --cluster "${CLUSTER}" \
    --service-name "${SERVICE##*/}" --desired-status STOPPED \
    --query 'taskArns[]' --output text 2>/dev/null | tr '\t' '\n' | head -10)"
if [ -z "${STOPPED}" ]; then
  note "no stopped tasks listed (none recently, or list-tasks denied)."
else
  # shellcheck disable=SC2086
  TDESC="$("${AWS[@]}" ecs describe-tasks --cluster "${CLUSTER}" \
      --tasks ${STOPPED} 2>/dev/null)"
  printf '%s' "${TDESC}" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for t in d.get('tasks',[]):
    print('  -', t.get('taskArn','').split('/')[-1],
          'stopCode=' + str(t.get('stopCode')),
          '| reason:', t.get('stoppedReason'))
    for c in t.get('containers',[]):
        if c.get('exitCode') is not None or c.get('reason'):
            print('      container', c.get('name'),
                  'exit=' + str(c.get('exitCode')),
                  (c.get('reason') or ''))
" 2>/dev/null || note "could not parse stopped tasks."
fi

# ---------------------------------------------------------------------------
# 6. ALB target health
# ---------------------------------------------------------------------------
section "ALB target health"
TG_ARNS="$(jqf "
print('\n'.join(lb.get('targetGroupArn','') for lb in d['services'][0].get('loadBalancers',[]) if lb.get('targetGroupArn')))
")"
if [ -z "${TG_ARNS}" ]; then
  note "service has no ALB target group (not behind an ALB, or not parsed)."
else
  while IFS= read -r tg; do
    [ -z "${tg}" ] && continue
    echo "  Target group: ${tg##*/}"
    "${AWS[@]}" elbv2 describe-target-health --target-group-arn "${tg}" 2>/dev/null \
      | python3 -c "
import sys,json
d=json.load(sys.stdin)
hs=d.get('TargetHealthDescriptions',[])
if not hs: print('    (no targets registered)')
for h in hs:
    th=h.get('TargetHealth',{})
    print('    -', h.get('Target',{}).get('Id'),
          th.get('State'),
          '|', th.get('Reason',''), th.get('Description',''))
" 2>/dev/null || note "describe-target-health failed for ${tg##*/}."
  done <<< "${TG_ARNS}"
fi

echo
echo "Done. Findings are hypotheses — correlate deploy times with the alarm (see recent-changes)."
