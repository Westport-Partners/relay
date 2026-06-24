#!/usr/bin/env bash
# database-connectivity/probe.sh — READ-ONLY RDS/Aurora connectivity diagnostics.
#
# Uses only describe*/list*/get* calls. Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION               required  AWS region
#   RELAY_APP_NAME             required  app name (used to discover DB identifier)
#   RELAY_DB_IDENTIFIER        optional  RDS instance id or Aurora cluster id
#   RELAY_APP_SECURITY_GROUP   optional  app/task SG id (sg-...) to test path to DB SG
#   RELAY_WINDOW_MINUTES       optional  lookback for metrics + events (default 60)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
APP="${RELAY_APP_NAME:-}"
DB_ID="${RELAY_DB_IDENTIFIER:-}"
APP_SG="${RELAY_APP_SECURITY_GROUP:-}"
WINDOW="${RELAY_WINDOW_MINUTES:-60}"

if [ -z "${REGION}" ] || [ -z "${APP}" ]; then
  echo "ERROR: RELAY_REGION and RELAY_APP_NAME are required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
NOW_EPOCH=$(date -u +%s)
START_EPOCH=$(( NOW_EPOCH - WINDOW * 60 ))
# ISO-8601 for rds describe-events --start-time
START_ISO=$(date -u -d "@${START_EPOCH}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
            || date -u -r "${START_EPOCH}" '+%Y-%m-%dT%H:%M:%SZ')
# ISO-8601 for CloudWatch (requires full RFC 3339)
CW_START="${START_ISO}"
CW_END=$(date -u -d "@${NOW_EPOCH}" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
         || date -u -r "${NOW_EPOCH}" '+%Y-%m-%dT%H:%M:%SZ')

# ---------------------------------------------------------------------------
# 1. Resolve DB instance / cluster
# ---------------------------------------------------------------------------
section "Resolution"

DB_TYPE=""   # "instance" or "cluster"
INSTANCE_JSON=""
CLUSTER_JSON=""

if [ -n "${DB_ID}" ]; then
  note "RELAY_DB_IDENTIFIER='${DB_ID}'; trying as instance first, then cluster."
  INSTANCE_JSON="$("${AWS[@]}" rds describe-db-instances \
      --db-instance-identifier "${DB_ID}" 2>/dev/null)"
  if [ -z "${INSTANCE_JSON}" ] || \
     ! python3 -c "import sys,json; d=json.loads(sys.stdin.read()); assert d.get('DBInstances')" \
       <<< "${INSTANCE_JSON}" 2>/dev/null; then
    INSTANCE_JSON=""
    CLUSTER_JSON="$("${AWS[@]}" rds describe-db-clusters \
        --db-cluster-identifier "${DB_ID}" 2>/dev/null)"
    if python3 -c "import sys,json; d=json.loads(sys.stdin.read()); assert d.get('DBClusters')" \
       <<< "${CLUSTER_JSON}" 2>/dev/null; then
      DB_TYPE="cluster"
      echo "  Resolved as Aurora cluster: ${DB_ID}"
    else
      CLUSTER_JSON=""
      note "DB_ID '${DB_ID}' not found as instance or cluster. Continuing with empty data."
    fi
  else
    DB_TYPE="instance"
    echo "  Resolved as RDS instance: ${DB_ID}"
  fi
else
  note "RELAY_DB_IDENTIFIER not set; discovering by app name '${APP}'."
  ALL_INSTANCES="$("${AWS[@]}" rds describe-db-instances 2>/dev/null)"
  MATCHED_INSTANCE="$(python3 -c "
import sys, json
app = '${APP}'.lower()
d = json.loads(sys.stdin.read())
for inst in d.get('DBInstances', []):
    iid = inst.get('DBInstanceIdentifier', '').lower()
    if app in iid:
        print(inst['DBInstanceIdentifier'])
        break
" <<< "${ALL_INSTANCES}" 2>/dev/null)"

  if [ -n "${MATCHED_INSTANCE}" ]; then
    DB_ID="${MATCHED_INSTANCE}"
    DB_TYPE="instance"
    INSTANCE_JSON="$("${AWS[@]}" rds describe-db-instances \
        --db-instance-identifier "${DB_ID}" 2>/dev/null)"
    echo "  Matched RDS instance by app name: ${DB_ID}"
  else
    note "No instance matched '${APP}'; trying Aurora clusters."
    ALL_CLUSTERS="$("${AWS[@]}" rds describe-db-clusters 2>/dev/null)"
    MATCHED_CLUSTER="$(python3 -c "
import sys, json
app = '${APP}'.lower()
d = json.loads(sys.stdin.read())
for cl in d.get('DBClusters', []):
    cid = cl.get('DBClusterIdentifier', '').lower()
    if app in cid:
        print(cl['DBClusterIdentifier'])
        break
" <<< "${ALL_CLUSTERS}" 2>/dev/null)"

    if [ -n "${MATCHED_CLUSTER}" ]; then
      DB_ID="${MATCHED_CLUSTER}"
      DB_TYPE="cluster"
      CLUSTER_JSON="$("${AWS[@]}" rds describe-db-clusters \
          --db-cluster-identifier "${DB_ID}" 2>/dev/null)"
      echo "  Matched Aurora cluster by app name: ${DB_ID}"
    else
      note "No RDS instance or Aurora cluster matched app name '${APP}'."
      note "Set RELAY_DB_IDENTIFIER explicitly if the DB name does not contain the app name."
      # Continue; subsequent sections will emit notes instead of data.
    fi
  fi
fi

[ -z "${DB_ID}" ] && note "DB identifier unknown; metric/event sections will be skipped."

# ---------------------------------------------------------------------------
# 2. DB status
# ---------------------------------------------------------------------------
section "DB status"

if [ -z "${DB_ID}" ]; then
  note "Skipped — no DB identifier resolved."
elif [ "${DB_TYPE}" = "instance" ] && [ -n "${INSTANCE_JSON}" ]; then
  python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
inst = d['DBInstances'][0]
status = inst.get('DBInstanceStatus', '?')
flag = '  *** STATUS NOT AVAILABLE ***' if status != 'available' else ''
print('  DBInstanceIdentifier :', inst.get('DBInstanceIdentifier'))
print('  DBInstanceStatus     :', status, flag)
print('  Engine               :', inst.get('Engine'), inst.get('EngineVersion'))
print('  DBInstanceClass      :', inst.get('DBInstanceClass'))
print('  MultiAZ              :', inst.get('MultiAZ'))
ep = inst.get('Endpoint') or {}
print('  Endpoint             :', ep.get('Address'), 'port', ep.get('Port'))
sgs = [sg.get('VpcSecurityGroupId') for sg in inst.get('VpcSecurityGroups', [])]
print('  VpcSecurityGroups    :', ', '.join(sgs) if sgs else '(none)')
" <<< "${INSTANCE_JSON}" 2>/dev/null || note "could not parse instance status."

elif [ "${DB_TYPE}" = "cluster" ] && [ -n "${CLUSTER_JSON}" ]; then
  python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
cl = d['DBClusters'][0]
status = cl.get('Status', '?')
flag = '  *** STATUS NOT AVAILABLE ***' if status != 'available' else ''
print('  DBClusterIdentifier  :', cl.get('DBClusterIdentifier'))
print('  Status               :', status, flag)
print('  Engine               :', cl.get('Engine'), cl.get('EngineVersion'))
print('  MultiAZ              :', cl.get('MultiAZ'))
print('  Endpoint (writer)    :', cl.get('Endpoint'), 'port', cl.get('Port'))
print('  ReaderEndpoint       :', cl.get('ReaderEndpoint'))
sgs = [sg.get('VpcSecurityGroupId') for sg in cl.get('VpcSecurityGroups', [])]
print('  VpcSecurityGroups    :', ', '.join(sgs) if sgs else '(none)')
" <<< "${CLUSTER_JSON}" 2>/dev/null || note "could not parse cluster status."
else
  note "No DB data to display."
fi

# ---------------------------------------------------------------------------
# Helper: extract DB SG ids and DB port for later sections
# ---------------------------------------------------------------------------
DB_SGS=""
DB_PORT=""
if [ -n "${INSTANCE_JSON}" ] && [ "${DB_TYPE}" = "instance" ]; then
  DB_SGS="$(python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
inst = d['DBInstances'][0]
sgs = [sg['VpcSecurityGroupId'] for sg in inst.get('VpcSecurityGroups', [])]
print(' '.join(sgs))
" <<< "${INSTANCE_JSON}" 2>/dev/null)"
  DB_PORT="$(python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
ep = d['DBInstances'][0].get('Endpoint') or {}
print(ep.get('Port', ''))
" <<< "${INSTANCE_JSON}" 2>/dev/null)"
elif [ -n "${CLUSTER_JSON}" ] && [ "${DB_TYPE}" = "cluster" ]; then
  DB_SGS="$(python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
cl = d['DBClusters'][0]
sgs = [sg['VpcSecurityGroupId'] for sg in cl.get('VpcSecurityGroups', [])]
print(' '.join(sgs))
" <<< "${CLUSTER_JSON}" 2>/dev/null)"
  DB_PORT="$(python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(d['DBClusters'][0].get('Port', ''))
" <<< "${CLUSTER_JSON}" 2>/dev/null)"
fi

# ---------------------------------------------------------------------------
# 3. Connection saturation (CloudWatch metrics)
# ---------------------------------------------------------------------------
section "Connection saturation (CloudWatch, last ${WINDOW}m)"

if [ -z "${DB_ID}" ]; then
  note "Skipped — no DB identifier resolved."
else
  # Period: use the full window, capped at 3600 s minimum for a single data point
  PERIOD=$(( WINDOW * 60 ))
  [ "${PERIOD}" -lt 60 ] && PERIOD=60

  cw_metric() {
    local metric_name="$1" stat="$2" unit="$3" label="$4"
    local raw
    raw="$("${AWS[@]}" cloudwatch get-metric-statistics \
        --namespace AWS/RDS \
        --metric-name "${metric_name}" \
        --dimensions Name=DBInstanceIdentifier,Value="${DB_ID}" \
        --start-time "${CW_START}" \
        --end-time "${CW_END}" \
        --period "${PERIOD}" \
        --statistics "${stat}" \
        2>/dev/null)"
    python3 -c "
import sys, json
label = '${label}'
stat  = '${stat}'
unit  = '${unit}'
data  = json.loads(sys.stdin.read())
pts   = data.get('Datapoints', [])
if not pts:
    print('  ' + label + ': (no data points in window)')
else:
    vals = [p[stat] for p in pts if stat in p]
    if not vals:
        print('  ' + label + ': (stat key missing)')
    else:
        peak = max(vals) if stat in ('Maximum', 'Average') else min(vals)
        print('  ' + label + ': ' + str(round(peak, 2)) + ' ' + unit)
" <<< "${raw}" 2>/dev/null || note "cloudwatch get-metric-statistics failed for ${metric_name}."
  }

  cw_metric "DatabaseConnections" "Maximum"  "connections" "DatabaseConnections (peak)"
  echo "  [note] max_connections is not a CloudWatch metric — it is derived from the"
  echo "         instance class and parameter group (e.g. ~660 for db.t3.medium,"
  echo "         ~3000+ for db.r6g.large). Compare the peak above against the"
  echo "         instance's known limit. If peak ≈ limit, pool exhaustion is likely."
  cw_metric "CPUUtilization"      "Average"  "%"           "CPUUtilization (avg)"
  cw_metric "FreeableMemory"      "Minimum"  "bytes"       "FreeableMemory (min)"
  cw_metric "FreeStorageSpace"    "Minimum"  "bytes"       "FreeStorageSpace (min)"
  echo "  [note] FreeStorageSpace < ~1 GiB (1073741824 bytes) or near zero can cause"
  echo "         RDS to set the instance read-only, blocking writes and new connections."
fi

# ---------------------------------------------------------------------------
# 4. Recent DB events
# ---------------------------------------------------------------------------
section "Recent DB events (last ${WINDOW}m)"

if [ -z "${DB_ID}" ]; then
  note "Skipped — no DB identifier resolved."
else
  SOURCE_TYPE="db-instance"
  [ "${DB_TYPE}" = "cluster" ] && SOURCE_TYPE="db-cluster"

  EVENTS="$("${AWS[@]}" rds describe-events \
      --source-identifier "${DB_ID}" \
      --source-type "${SOURCE_TYPE}" \
      --start-time "${CW_START}" \
      --end-time "${CW_END}" \
      2>/dev/null)"
  python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
evts = d.get('Events', [])
if not evts:
    print('  (no events in window)')
else:
    for e in evts:
        cats = ', '.join(e.get('EventCategories', []))
        print('  -', e.get('Date', '?'), '[' + cats + ']', e.get('Message', ''))
" <<< "${EVENTS}" 2>/dev/null || note "rds describe-events failed (permission or not-found)."
fi

# ---------------------------------------------------------------------------
# 5. Security-group path (DB SG inbound → app SG)
# ---------------------------------------------------------------------------
section "Security-group path"

if [ -z "${DB_SGS}" ]; then
  note "No DB security groups resolved; skipping path check."
else
  for sg_id in ${DB_SGS}; do
    echo "  DB security group: ${sg_id}"
    SG_JSON="$("${AWS[@]}" ec2 describe-security-groups \
        --group-ids "${sg_id}" 2>/dev/null)"
    if [ -z "${SG_JSON}" ]; then
      note "ec2 describe-security-groups failed for ${sg_id} (permission?)."
      continue
    fi

    python3 -c "
import sys, json
sg_json  = sys.stdin.read()
db_port  = '${DB_PORT}'
app_sg   = '${APP_SG}'

d = json.loads(sg_json)
sgs = d.get('SecurityGroups', [])
if not sgs:
    print('  [note] no security group data returned.')
    sys.exit(0)

sg = sgs[0]
print('  SG name:', sg.get('GroupName'), '| VPC:', sg.get('VpcId'))

if not db_port:
    print('  [note] DB port unknown; printing all inbound rules.')

port_int = int(db_port) if db_port else None
relevant = []
for rule in sg.get('IpPermissions', []):
    from_p = rule.get('FromPort')
    to_p   = rule.get('ToPort')
    proto  = rule.get('IpProtocol', '')
    # -1 = all traffic; include if port matches or all-traffic
    if proto == '-1' or port_int is None or (
            from_p is not None and to_p is not None
            and from_p <= port_int <= to_p):
        relevant.append(rule)

if not relevant:
    print('  [note] No inbound rules match DB port', db_port or '(unknown)', '— traffic is BLOCKED.')
else:
    print('  Inbound rules matching DB port', db_port or '(all):')
    path_found = False
    for rule in relevant:
        proto     = rule.get('IpProtocol', '?')
        from_p    = rule.get('FromPort', '*')
        to_p      = rule.get('ToPort', '*')
        port_desc = str(from_p) + ('-' + str(to_p) if to_p != from_p else '')
        for pair in rule.get('UserIdGroupPairs', []):
            src_sg = pair.get('GroupId', '?')
            print('    - proto=' + proto + ' port=' + port_desc + ' from SG=' + src_sg)
            if app_sg and src_sg == app_sg:
                path_found = True
        for cidr in rule.get('IpRanges', []):
            print('    - proto=' + proto + ' port=' + port_desc + ' from CIDR=' + cidr.get('CidrIp', '?'))
        for cidr6 in rule.get('Ipv6Ranges', []):
            print('    - proto=' + proto + ' port=' + port_desc + ' from CIDRv6=' + cidr6.get('CidrIpv6', '?'))
    if app_sg:
        if path_found:
            print()
            print('  RESULT: App SG', app_sg, 'IS present in DB SG inbound rules — path appears open.')
            print('  [note] If the app still cannot connect, check NACLs/route tables via network-connectivity.')
        else:
            print()
            print('  RESULT: App SG', app_sg, 'NOT found in DB SG inbound rules — path appears BLOCKED.')
            print('  [note] Hypothesis: missing SG rule is preventing connectivity. Pivot to network-connectivity for NACL/route analysis.')
    else:
        print()
        print('  [note] RELAY_APP_SECURITY_GROUP not set; cannot determine path automatically.')
        print('  [note] Inspect the inbound rules above manually, or re-run with RELAY_APP_SECURITY_GROUP=sg-...')
" <<< "${SG_JSON}" 2>/dev/null || note "could not parse security group ${sg_id}."
  done
fi

echo
echo "Done. Findings are hypotheses — correlate with recent DB events, deploy times,"
echo "and app error logs (see recent-changes and cloudwatch-alarm-context)."
