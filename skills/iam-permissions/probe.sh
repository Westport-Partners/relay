#!/usr/bin/env bash
# iam-permissions/probe.sh — READ-ONLY IAM permission diagnostics.
#
# Uses only get*/list*/lookup-events/simulate-principal-policy calls.
# Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION          required  AWS region
#   RELAY_PRINCIPAL_ARN   optional  role or user ARN being denied
#                                   (else discovered from CloudTrail denials)
#   RELAY_DENIED_ACTION   optional  IAM action to simulate, e.g. s3:GetObject
#                                   (else derived from first CloudTrail denial)
#   RELAY_RESOURCE_ARN    optional  resource ARN for simulation (default *)
#   RELAY_WINDOW_MINUTES  optional  CloudTrail lookback window (default 60)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
PRINCIPAL="${RELAY_PRINCIPAL_ARN:-}"
ACTION="${RELAY_DENIED_ACTION:-}"
RESOURCE="${RELAY_RESOURCE_ARN:-*}"
WINDOW="${RELAY_WINDOW_MINUTES:-60}"

if [ -z "${REGION}" ]; then
  echo "ERROR: RELAY_REGION is required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Resolution
# ---------------------------------------------------------------------------
section "Resolution"
echo "  RELAY_REGION          : ${REGION}"
echo "  RELAY_PRINCIPAL_ARN   : ${PRINCIPAL:-<not set — will discover from CloudTrail>}"
echo "  RELAY_DENIED_ACTION   : ${ACTION:-<not set — will derive from first denial>}"
echo "  RELAY_RESOURCE_ARN    : ${RESOURCE}"
echo "  RELAY_WINDOW_MINUTES  : ${WINDOW}"

# Compute the start time for CloudTrail lookback.
START_TIME="$(date -u -d "-${WINDOW} minutes" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || {
  note "date -d flag not supported on this platform; trying macOS syntax."
  START_TIME="$(date -u -v-"${WINDOW}"M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || {
    note "Could not compute start time; defaulting to 60-minute window via epoch math."
    START_TIME="$(python3 -c "
import datetime
t = datetime.datetime.utcnow() - datetime.timedelta(minutes=int('${WINDOW}'))
print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))
")"
  }
}
echo "  CloudTrail start time : ${START_TIME}"

# ---------------------------------------------------------------------------
# 2. Recent denied calls (CloudTrail)
# ---------------------------------------------------------------------------
section "Recent denied calls (CloudTrail, last ${WINDOW}m)"

CT_RAW="$("${AWS[@]}" cloudtrail lookup-events \
    --start-time "${START_TIME}" \
    --max-results 50 2>/dev/null)" || {
  note "cloudtrail lookup-events failed (permission denied or service unavailable)."
  CT_RAW=""
}

if [ -n "${CT_RAW}" ]; then
  # CloudTrail cannot server-side filter on errorCode; filter client-side.
  # Each event has a CloudTrailEvent field that is itself a JSON string —
  # it must be json.loads()'d a second time to access errorCode/errorMessage.
  CT_DENIED="$(printf '%s' "${CT_RAW}" | python3 -c "
import sys, json

data = json.load(sys.stdin)
events = data.get('Events', [])
found = []
for ev in events:
    raw = ev.get('CloudTrailEvent', '{}')
    try:
        detail = json.loads(raw)
    except (ValueError, TypeError):
        continue
    ec = detail.get('errorCode', '')
    if 'AccessDenied' in ec or 'Unauthorized' in ec:
        t   = ev.get('EventTime', '')
        name = ev.get('EventName', '')
        uid  = detail.get('userIdentity', {})
        prin = uid.get('arn') or uid.get('userName') or uid.get('type') or 'unknown'
        em   = detail.get('errorMessage', '')
        resources = detail.get('resources', [])
        res_arns = ', '.join(r.get('ARN', '') for r in resources if r.get('ARN'))
        found.append((str(t), name, prin, ec, em, res_arns))

if not found:
    print('  (no AccessDenied / Unauthorized events in window)')
else:
    for t, name, prin, ec, em, res in found:
        print('  ---')
        print('  time      :', t)
        print('  EventName :', name)
        print('  principal :', prin)
        print('  errorCode :', ec)
        print('  errorMsg  :', em)
        if res:
            print('  resources :', res)
" 2>/dev/null)" || {
    note "Failed to parse CloudTrail events."
    CT_DENIED=""
  }

  echo "${CT_DENIED:-  (no denied events found or parse failed)}"

  # If PRINCIPAL not supplied, derive from first denied event.
  if [ -z "${PRINCIPAL}" ]; then
    DISCOVERED_PRINCIPAL="$(printf '%s' "${CT_RAW}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for ev in data.get('Events', []):
    try:
        detail = json.loads(ev.get('CloudTrailEvent', '{}'))
    except (ValueError, TypeError):
        continue
    ec = detail.get('errorCode', '')
    if 'AccessDenied' in ec or 'Unauthorized' in ec:
        uid = detail.get('userIdentity', {})
        p = uid.get('arn') or uid.get('userName') or ''
        if p:
            print(p)
            break
" 2>/dev/null)"
    if [ -n "${DISCOVERED_PRINCIPAL}" ]; then
      PRINCIPAL="${DISCOVERED_PRINCIPAL}"
      note "Discovered principal from first denied event: ${PRINCIPAL}"
    else
      note "No principal ARN discoverable from CloudTrail; skipping sections 3 and 4."
    fi
  fi

  # If ACTION not supplied, derive from first denied event.
  if [ -z "${ACTION}" ]; then
    DISCOVERED_ACTION="$(printf '%s' "${CT_RAW}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for ev in data.get('Events', []):
    try:
        detail = json.loads(ev.get('CloudTrailEvent', '{}'))
    except (ValueError, TypeError):
        continue
    ec = detail.get('errorCode', '')
    if 'AccessDenied' in ec or 'Unauthorized' in ec:
        svc = detail.get('eventSource', '').replace('.amazonaws.com', '')
        name = ev.get('EventName', '')
        if svc and name:
            print(svc + ':' + name)
            break
" 2>/dev/null)"
    if [ -n "${DISCOVERED_ACTION}" ]; then
      ACTION="${DISCOVERED_ACTION}"
      note "Derived action from first denied event: ${ACTION}"
    else
      note "Could not derive action; simulation (section 4) will be skipped."
    fi
  fi
else
  note "No CloudTrail data available; skipping denial discovery."
fi

# ---------------------------------------------------------------------------
# 3. Principal policies
# ---------------------------------------------------------------------------
section "Principal policies"

if [ -z "${PRINCIPAL}" ]; then
  note "No principal ARN available (set RELAY_PRINCIPAL_ARN or ensure CloudTrail has denied events). Skipping."
else
  # Determine whether this is a role or a user from the ARN shape.
  PRINCIPAL_TYPE="unknown"
  if printf '%s' "${PRINCIPAL}" | grep -q ':role/'; then
    PRINCIPAL_TYPE="role"
  elif printf '%s' "${PRINCIPAL}" | grep -q ':user/'; then
    PRINCIPAL_TYPE="user"
  elif printf '%s' "${PRINCIPAL}" | grep -q ':assumed-role/'; then
    # Strip session suffix to get the role ARN for policy lookups.
    PRINCIPAL_TYPE="assumed-role"
    ROLE_NAME="$(printf '%s' "${PRINCIPAL}" | python3 -c "
import sys
arn = sys.stdin.read().strip()
# arn:aws:sts::ACCOUNT:assumed-role/ROLE-NAME/SESSION
parts = arn.split('/')
print(parts[1] if len(parts) >= 2 else '')
" 2>/dev/null)"
    note "Principal is an assumed-role session; looking up role '${ROLE_NAME}'."
    PRINCIPAL_TYPE="role"
    # Reconstruct the role ARN for get-role.
    ACCOUNT_ID="$(printf '%s' "${PRINCIPAL}" | python3 -c "
import sys
arn = sys.stdin.read().strip()
print(arn.split(':')[4])
" 2>/dev/null)"
    PRINCIPAL="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
    note "Resolved role ARN: ${PRINCIPAL}"
  fi

  ROLE_OR_USER_NAME="$(printf '%s' "${PRINCIPAL}" | python3 -c "
import sys; arn=sys.stdin.read().strip(); print(arn.split('/')[-1])
" 2>/dev/null)"

  if [ "${PRINCIPAL_TYPE}" = "role" ]; then
    echo "  Principal type: role  (${ROLE_OR_USER_NAME})"

    echo ""
    echo "  -- Role metadata --"
    "${AWS[@]}" iam get-role --role-name "${ROLE_OR_USER_NAME}" 2>/dev/null \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
r = d.get('Role', {})
print('  RoleId         :', r.get('RoleId'))
print('  Path           :', r.get('Path'))
print('  MaxSessionDur  :', r.get('MaxSessionDuration'))
pb = r.get('PermissionsBoundary', {})
if pb:
    print('  PermBoundary   :', pb.get('PermissionsBoundaryArn'))
else:
    print('  PermBoundary   : (none)')
" 2>/dev/null || note "get-role failed for '${ROLE_OR_USER_NAME}'."

    echo ""
    echo "  -- Attached managed policies --"
    ATTACHED="$("${AWS[@]}" iam list-attached-role-policies \
        --role-name "${ROLE_OR_USER_NAME}" 2>/dev/null)" || { note "list-attached-role-policies failed."; ATTACHED=""; }
    if [ -n "${ATTACHED}" ]; then
      printf '%s' "${ATTACHED}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
pols = d.get('AttachedPolicies', [])
if not pols:
    print('  (none)')
for p in pols:
    print('  -', p.get('PolicyName'), '|', p.get('PolicyArn'))
" 2>/dev/null || note "Could not parse attached policies."
    fi

    echo ""
    echo "  -- Inline policies --"
    INLINE_NAMES="$("${AWS[@]}" iam list-role-policies \
        --role-name "${ROLE_OR_USER_NAME}" 2>/dev/null)" || { note "list-role-policies failed."; INLINE_NAMES=""; }
    if [ -n "${INLINE_NAMES}" ]; then
      mapfile -t POLICIES < <(printf '%s' "${INLINE_NAMES}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for n in d.get('PolicyNames', []):
    print(n)
" 2>/dev/null)
      if [ "${#POLICIES[@]}" -eq 0 ]; then
        echo "  (no inline policies)"
      fi
      for pname in "${POLICIES[@]}"; do
        [ -z "${pname}" ] && continue
        echo ""
        echo "  Inline policy: ${pname}"
        "${AWS[@]}" iam get-role-policy \
            --role-name "${ROLE_OR_USER_NAME}" \
            --policy-name "${pname}" 2>/dev/null \
          | python3 -c "
import sys, json
d = json.load(sys.stdin)
doc = d.get('PolicyDocument', {})
print(json.dumps(doc, indent=4))
" 2>/dev/null || note "get-role-policy failed for '${pname}'."
      done
    fi

  elif [ "${PRINCIPAL_TYPE}" = "user" ]; then
    echo "  Principal type: user  (${ROLE_OR_USER_NAME})"

    echo ""
    echo "  -- User metadata --"
    "${AWS[@]}" iam get-user --user-name "${ROLE_OR_USER_NAME}" 2>/dev/null \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
u = d.get('User', {})
print('  UserId     :', u.get('UserId'))
print('  Path       :', u.get('Path'))
pb = u.get('PermissionsBoundary', {})
if pb:
    print('  PermBoundary:', pb.get('PermissionsBoundaryArn'))
else:
    print('  PermBoundary: (none)')
" 2>/dev/null || note "get-user failed for '${ROLE_OR_USER_NAME}'."

    echo ""
    echo "  -- Attached managed policies --"
    "${AWS[@]}" iam list-attached-user-policies \
        --user-name "${ROLE_OR_USER_NAME}" 2>/dev/null \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
pols = d.get('AttachedPolicies', [])
if not pols:
    print('  (none)')
for p in pols:
    print('  -', p.get('PolicyName'), '|', p.get('PolicyArn'))
" 2>/dev/null || note "Could not parse attached user policies."

    echo ""
    echo "  -- Inline policies --"
    INLINE_NAMES="$("${AWS[@]}" iam list-user-policies \
        --user-name "${ROLE_OR_USER_NAME}" 2>/dev/null)" || { note "list-user-policies failed."; INLINE_NAMES=""; }
    if [ -n "${INLINE_NAMES}" ]; then
      mapfile -t POLICIES < <(printf '%s' "${INLINE_NAMES}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for n in d.get('PolicyNames', []):
    print(n)
" 2>/dev/null)
      if [ "${#POLICIES[@]}" -eq 0 ]; then
        echo "  (no inline policies)"
      fi
      for pname in "${POLICIES[@]}"; do
        [ -z "${pname}" ] && continue
        echo ""
        echo "  Inline policy: ${pname}"
        "${AWS[@]}" iam get-user-policy \
            --user-name "${ROLE_OR_USER_NAME}" \
            --policy-name "${pname}" 2>/dev/null \
          | python3 -c "
import sys, json
d = json.load(sys.stdin)
doc = d.get('PolicyDocument', {})
print(json.dumps(doc, indent=4))
" 2>/dev/null || note "get-user-policy failed for '${pname}'."
      done
    fi

  else
    note "Cannot determine whether principal is a role or user from ARN '${PRINCIPAL}'. Skipping policy lookup."
  fi
fi

# ---------------------------------------------------------------------------
# 4. Policy simulation
# ---------------------------------------------------------------------------
section "Policy simulation (simulate-principal-policy)"

if [ -z "${PRINCIPAL}" ]; then
  note "No principal ARN; skipping simulation."
elif [ -z "${ACTION}" ]; then
  note "No action to simulate (set RELAY_DENIED_ACTION or ensure CloudTrail reveals it). Skipping."
else
  echo "  Principal : ${PRINCIPAL}"
  echo "  Action    : ${ACTION}"
  echo "  Resource  : ${RESOURCE}"
  echo ""
  SIM="$("${AWS[@]}" iam simulate-principal-policy \
      --policy-source-arn "${PRINCIPAL}" \
      --action-names "${ACTION}" \
      --resource-arns "${RESOURCE}" 2>/dev/null)" || {
    note "simulate-principal-policy failed (permission denied or invalid ARN)."
    SIM=""
  }

  if [ -n "${SIM}" ]; then
    printf '%s' "${SIM}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
results = d.get('EvaluationResults', [])
if not results:
    print('  (no evaluation results returned)')
for r in results:
    print('  EvalDecision :', r.get('EvalDecision'))
    print('  ActionName   :', r.get('EvalActionName'))
    print('  ResourceName :', r.get('EvalResourceName'))
    matched = r.get('MatchedStatements', [])
    if matched:
        print('  MatchedStatements:')
        for s in matched:
            print('    -', s.get('SourcePolicyId'), '|', s.get('SourcePolicyType'),
                  '| startLine:', s.get('StartPosition', {}).get('Line'),
                  '| endLine:', s.get('EndPosition', {}).get('Line'))
    else:
        print('  MatchedStatements: (none — implicitly denied by lack of Allow)')
    missing = r.get('MissingContextValues', [])
    if missing:
        print('  MissingContextValues (simulation may be incomplete):', missing)
    boundary = r.get('PermissionsBoundaryDecisionDetail', {})
    if boundary:
        print('  PermBoundaryAllowed:', boundary.get('AllowedByPermissionsBoundary'))
" 2>/dev/null || note "Could not parse simulation results."
  fi
fi

echo
echo "Done. Findings are hypotheses — check SKILL.md interpretation section for next steps."
echo "Reminder: this account family uses inline-only policies on pre-provisioned roles;"
echo "remediation should add an inline statement, not create a new role."
