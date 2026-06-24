#!/usr/bin/env bash
# network-connectivity/probe.sh — READ-ONLY reachability analysis.
#
# Uses only describe* calls. Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION       required  AWS region
#   RELAY_SOURCE_SG    optional  source security group ID (e.g. ECS task SG)
#   RELAY_TARGET_SG    optional  target security group ID (e.g. DB or dependency SG)
#   RELAY_TARGET_PORT  optional  port traffic should reach (default 443)
#   RELAY_SUBNET_IDS   optional  comma-separated subnet IDs to inspect; else discovered
#   RELAY_VPC_ID       optional  VPC to scope; else derived from provided SGs
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
SOURCE_SG="${RELAY_SOURCE_SG:-}"
TARGET_SG="${RELAY_TARGET_SG:-}"
TARGET_PORT="${RELAY_TARGET_PORT:-443}"
SUBNET_IDS_CSV="${RELAY_SUBNET_IDS:-}"
VPC_ID="${RELAY_VPC_ID:-}"

if [ -z "${REGION}" ]; then
  echo "ERROR: RELAY_REGION is required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Resolution — what we're analyzing
# ---------------------------------------------------------------------------
section "Resolution"
echo "  Region      : ${REGION}"
echo "  Source SG   : ${SOURCE_SG:-'(not provided)'}"
echo "  Target SG   : ${TARGET_SG:-'(not provided)'}"
echo "  Target port : ${TARGET_PORT}"

# Derive VPC from SG(s) if not provided
if [ -z "${VPC_ID}" ] && { [ -n "${SOURCE_SG}" ] || [ -n "${TARGET_SG}" ]; }; then
  PROBE_SG="${TARGET_SG:-${SOURCE_SG}}"
  VPC_RAW="$("${AWS[@]}" ec2 describe-security-groups \
      --group-ids "${PROBE_SG}" \
      --query 'SecurityGroups[0].VpcId' --output text 2>/dev/null)"
  if [ -n "${VPC_RAW}" ] && [ "${VPC_RAW}" != "None" ]; then
    VPC_ID="${VPC_RAW}"
    note "VPC derived from SG ${PROBE_SG}: ${VPC_ID}"
  else
    note "Could not derive VPC from provided SG(s)."
  fi
fi

[ -n "${VPC_ID}" ] && echo "  VPC         : ${VPC_ID}" || echo "  VPC         : (unknown)"

# Resolve subnets if not provided
if [ -z "${SUBNET_IDS_CSV}" ] && [ -n "${VPC_ID}" ]; then
  note "RELAY_SUBNET_IDS not set; discovering subnets in VPC ${VPC_ID}."
  SUBNET_IDS_CSV="$("${AWS[@]}" ec2 describe-subnets \
      --filters "Name=vpc-id,Values=${VPC_ID}" \
      --query 'Subnets[].SubnetId' \
      --output text 2>/dev/null | tr '\t' ',')"
  if [ -n "${SUBNET_IDS_CSV}" ]; then
    note "Discovered subnets: ${SUBNET_IDS_CSV}"
  else
    note "No subnets discovered for VPC ${VPC_ID}."
  fi
fi

echo "  Subnets     : ${SUBNET_IDS_CSV:-'(none resolved)'}"

if [ -z "${SOURCE_SG}" ] && [ -z "${TARGET_SG}" ]; then
  note "No SG IDs provided. Sections 2 (SG rules) will be skipped."
fi
if [ -z "${SUBNET_IDS_CSV}" ]; then
  note "No subnets resolved. Sections 3 (NACLs) and 4 (route tables) will be skipped."
fi
if [ -z "${VPC_ID}" ]; then
  note "No VPC resolved. Section 5 (VPC endpoints) will be skipped."
fi

# ---------------------------------------------------------------------------
# 2. Security group rules
# ---------------------------------------------------------------------------
section "Security group rules"

if [ -z "${SOURCE_SG}" ] && [ -z "${TARGET_SG}" ]; then
  note "Skipping: no SG IDs supplied (set RELAY_SOURCE_SG / RELAY_TARGET_SG)."
else
  # Build a space-separated list of SG IDs to describe together
  SG_LIST=""
  [ -n "${TARGET_SG}" ] && SG_LIST="${SG_LIST} ${TARGET_SG}"
  [ -n "${SOURCE_SG}" ] && SG_LIST="${SG_LIST} ${SOURCE_SG}"
  SG_LIST="${SG_LIST# }"   # trim leading space

  # shellcheck disable=SC2086
  SG_DESC="$("${AWS[@]}" ec2 describe-security-groups \
      --group-ids ${SG_LIST} 2>/dev/null)" || SG_DESC=""

  if [ -z "${SG_DESC}" ]; then
    note "describe-security-groups failed (permission denied or SG not found)."
  else
    # --- TARGET SG: inbound rules ---
    if [ -n "${TARGET_SG}" ]; then
      echo ""
      echo "  TARGET SG (${TARGET_SG}) — inbound rules for port ${TARGET_PORT}:"
      printf '%s' "${SG_DESC}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
target = '${TARGET_SG}'
port   = ${TARGET_PORT}
source = '${SOURCE_SG}'

sg = next((g for g in d.get('SecurityGroups', []) if g['GroupId'] == target), None)
if not sg:
    print('  [note] target SG not found in describe output.')
    sys.exit(0)

inbound = sg.get('IpPermissions', [])
found_allow = False
for rule in inbound:
    from_p = rule.get('FromPort', -1)
    to_p   = rule.get('ToPort', -1)
    proto  = rule.get('IpProtocol', '')
    # protocol -1 means all traffic
    covers_port = (proto == '-1') or (isinstance(from_p, int) and from_p <= port <= to_p)
    if not covers_port:
        continue
    # collect what's allowed
    cidrs  = [r.get('CidrIp', '') for r in rule.get('IpRanges', [])]
    cidrs6 = [r.get('CidrIpv6', '') for r in rule.get('Ipv6Ranges', [])]
    sgs    = [p.get('GroupId', '') for p in rule.get('UserIdGroupPairs', [])]
    allows_source = (
        '0.0.0.0/0' in cidrs or '::/0' in cidrs6 or
        (source and source in sgs)
    )
    if allows_source:
        found_allow = True
    tag = 'ALLOW (covers source)' if allows_source else 'allows other sources'
    print('    rule proto=%s ports=%s-%s cidrs=%s sgs=%s  => %s' % (
        proto, from_p, to_p,
        ','.join(cidrs + cidrs6) or '(none)',
        ','.join(sgs) or '(none)',
        tag))
if found_allow:
    print('  RESULT: target SG HAS an inbound rule allowing port %d from the source.' % port)
else:
    print('  RESULT: target SG has NO inbound rule allowing port %d from source SG %s.' % (port, source or '(unknown)'))
    print('          ** SG is likely BLOCKING the connection. **')
" 2>/dev/null || note "Could not parse target SG inbound rules."
    fi

    # --- SOURCE SG: outbound rules ---
    if [ -n "${SOURCE_SG}" ]; then
      echo ""
      echo "  SOURCE SG (${SOURCE_SG}) — egress rules (checking port ${TARGET_PORT}):"
      printf '%s' "${SG_DESC}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
source = '${SOURCE_SG}'
port   = ${TARGET_PORT}

sg = next((g for g in d.get('SecurityGroups', []) if g['GroupId'] == source), None)
if not sg:
    print('  [note] source SG not found in describe output.')
    sys.exit(0)

egress = sg.get('IpPermissionsEgress', [])
if not egress:
    print('  [note] source SG has no egress rules (all outbound blocked).')
    sys.exit(0)

found_allow = False
for rule in egress:
    from_p = rule.get('FromPort', -1)
    to_p   = rule.get('ToPort', -1)
    proto  = rule.get('IpProtocol', '')
    covers_port = (proto == '-1') or (isinstance(from_p, int) and from_p <= port <= to_p)
    cidrs  = [r.get('CidrIp', '') for r in rule.get('IpRanges', [])]
    cidrs6 = [r.get('CidrIpv6', '') for r in rule.get('Ipv6Ranges', [])]
    sgs    = [p.get('GroupId', '') for p in rule.get('UserIdGroupPairs', [])]
    is_all = '0.0.0.0/0' in cidrs or '::/0' in cidrs6
    tag = ''
    if covers_port and is_all:
        found_allow = True
        tag = 'ALLOWS all outbound (default)'
    elif covers_port:
        found_allow = True
        tag = 'ALLOWS port %d to restricted destinations' % port
    print('    rule proto=%s ports=%s-%s cidrs=%s sgs=%s  %s' % (
        proto, from_p, to_p,
        ','.join(cidrs + cidrs6) or '(none)',
        ','.join(sgs) or '(none)',
        tag))
if found_allow:
    print('  RESULT: source SG egress allows port %d outbound.' % port)
else:
    print('  RESULT: source SG egress does NOT allow port %d.' % port)
    print('          ** Egress SG may be BLOCKING the connection. **')
" 2>/dev/null || note "Could not parse source SG egress rules."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 3. NACLs — stateless; must check BOTH directions including ephemeral ports
# ---------------------------------------------------------------------------
section "NACLs (target subnets)"

if [ -z "${SUBNET_IDS_CSV}" ]; then
  note "Skipping: no subnets to inspect."
else
  # Convert CSV to space-separated for filter
  SUBNET_FILTER_VALS="${SUBNET_IDS_CSV//,/ }"

  # shellcheck disable=SC2086
  NACL_DESC="$("${AWS[@]}" ec2 describe-network-acls \
      --filters "Name=association.subnet-id,Values=${SUBNET_IDS_CSV}" \
      2>/dev/null)" || NACL_DESC=""

  if [ -z "${NACL_DESC}" ]; then
    note "describe-network-acls returned nothing (permission denied or no NACLs associated)."
  else
    printf '%s' "${NACL_DESC}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
port = ${TARGET_PORT}
EPHEMERAL_LOW  = 1024
EPHEMERAL_HIGH = 65535

def covers(entry, lo, hi):
    pr = entry.get('Protocol', '-1')
    if pr == '-1':
        return True
    pr_r = entry.get('PortRange')
    if not pr_r:
        return False
    return pr_r.get('From', 0) <= hi and pr_r.get('To', 65535) >= lo

def fmt_entry(e):
    action = 'ALLOW' if e.get('RuleAction') == 'allow' else 'DENY'
    pr = e.get('Protocol', '?')
    pr_r = e.get('PortRange')
    ports = ('%d-%d' % (pr_r['From'], pr_r['To'])) if pr_r else 'all'
    cidr = e.get('CidrBlock', e.get('Ipv6CidrBlock', '?'))
    return '#%d %s %s proto=%s ports=%s cidr=%s' % (
        e.get('RuleNumber', 0), action, 'IN' if e.get('Egress') == False else 'OUT',
        pr, ports, cidr)

for nacl in d.get('NetworkAcls', []):
    nacl_id  = nacl.get('NetworkAclId')
    assocs   = [a.get('SubnetId') for a in nacl.get('Associations', [])]
    entries  = nacl.get('Entries', [])
    inbound  = [e for e in entries if not e.get('Egress', True)]
    outbound = [e for e in entries if e.get('Egress', False)]

    # relevant inbound: target port
    rel_in_port  = [e for e in inbound  if covers(e, port, port)]
    # relevant outbound: ephemeral return traffic
    rel_out_eph  = [e for e in outbound if covers(e, EPHEMERAL_LOW, EPHEMERAL_HIGH)]
    # relevant inbound: ephemeral (return traffic from target, source perspective)
    rel_in_eph   = [e for e in inbound  if covers(e, EPHEMERAL_LOW, EPHEMERAL_HIGH)]

    print()
    print('  NACL:', nacl_id, ' subnets:', assocs)

    print('  Inbound rules covering port %d:' % port)
    if rel_in_port:
        for e in sorted(rel_in_port, key=lambda x: x.get('RuleNumber', 999)):
            print('    ', fmt_entry(e))
    else:
        print('    (none — all traffic on port %d is implicitly denied)' % port)
        print('    ** NACL may be BLOCKING inbound port %d **' % port)

    print('  Outbound rules covering ephemeral return ports (%d-%d):' % (EPHEMERAL_LOW, EPHEMERAL_HIGH))
    if rel_out_eph:
        for e in sorted(rel_out_eph, key=lambda x: x.get('RuleNumber', 999)):
            print('    ', fmt_entry(e))
    else:
        print('    (none — no outbound ephemeral-return path)')
        print('    ** NACL stateless gap: replies to port %d will be DROPPED **' % port)

    # warn if a DENY has lower rule number than the first ALLOW
    for direction, relevant in [('inbound', rel_in_port), ('outbound', rel_out_eph)]:
        sorted_r = sorted(relevant, key=lambda x: x.get('RuleNumber', 999))
        deny_nums  = [e.get('RuleNumber', 999) for e in sorted_r if e.get('RuleAction') == 'deny']
        allow_nums = [e.get('RuleNumber', 999) for e in sorted_r if e.get('RuleAction') == 'allow']
        if deny_nums and allow_nums and min(deny_nums) < min(allow_nums):
            print('  ** %s: DENY rule #%d precedes ALLOW rule #%d — DENY wins **' % (
                direction, min(deny_nums), min(allow_nums)))
" 2>/dev/null || note "Could not parse NACL output."
  fi
fi

# ---------------------------------------------------------------------------
# 4. Route tables — is there a path out of the subnet?
# ---------------------------------------------------------------------------
section "Route tables"

if [ -z "${SUBNET_IDS_CSV}" ]; then
  note "Skipping: no subnets to inspect."
else
  # shellcheck disable=SC2086
  RT_DESC="$("${AWS[@]}" ec2 describe-route-tables \
      --filters "Name=association.subnet-id,Values=${SUBNET_IDS_CSV}" \
      2>/dev/null)" || RT_DESC=""

  # Also try main route table for the VPC if no explicit associations found
  if { [ -z "${RT_DESC}" ] || python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('RouteTables') else 1)" <<< "${RT_DESC}" 2>/dev/null; } && [ -n "${VPC_ID}" ]; then
    note "No subnet-associated route tables found; falling back to VPC main route table."
    RT_DESC="$("${AWS[@]}" ec2 describe-route-tables \
        --filters "Name=vpc-id,Values=${VPC_ID}" "Name=association.main,Values=true" \
        2>/dev/null)" || RT_DESC=""
  fi

  if [ -z "${RT_DESC}" ]; then
    note "describe-route-tables returned nothing (permission denied or no tables found)."
  else
    printf '%s' "${RT_DESC}" | python3 -c "
import sys, json
d = json.load(sys.stdin)

GW_TYPES = {
    'igw': 'Internet Gateway (public subnet)',
    'nat': 'NAT Gateway (private→internet)',
    'tgw': 'Transit Gateway',
    'vgw': 'Virtual Private Gateway (VPN)',
    'pcx': 'VPC Peering',
    'vpce': 'VPC Endpoint',
    'local': 'local (VPC CIDR — in-VPC only)',
}

def classify(route):
    for key, label in GW_TYPES.items():
        for field in ('GatewayId', 'NatGatewayId', 'TransitGatewayId',
                      'VpcPeeringConnectionId', 'VpcEndpointId',
                      'NetworkInterfaceId', 'InstanceId'):
            val = route.get(field, '') or ''
            if val.startswith(key):
                return label
    return 'unknown target'

for rt in d.get('RouteTables', []):
    rt_id   = rt.get('RouteTableId')
    assocs  = [a.get('SubnetId', '(main)') for a in rt.get('Associations', [])]
    routes  = rt.get('Routes', [])
    print()
    print('  Route table:', rt_id, ' subnets:', assocs)
    has_default = False
    for r in routes:
        dest   = r.get('DestinationCidrBlock') or r.get('DestinationIpv6CidrBlock') or r.get('DestinationPrefixListId', '?')
        state  = r.get('State', '?')
        target = classify(r)
        is_default = dest in ('0.0.0.0/0', '::/0')
        if is_default:
            has_default = True
        print('    %s  state=%s  via %s' % (dest, state, target))
    if not has_default:
        print('  ** No default route (0.0.0.0/0) — truly isolated private subnet. **')
        print('     Cannot reach internet or AWS services without a NAT gateway or VPC endpoint.')
    else:
        print('  Default route present.')
" 2>/dev/null || note "Could not parse route table output."
  fi
fi

# ---------------------------------------------------------------------------
# 5. VPC endpoints — needed for AWS services in no-NAT private subnets
# ---------------------------------------------------------------------------
section "VPC endpoints"

if [ -z "${VPC_ID}" ]; then
  note "Skipping: VPC ID not resolved."
else
  VPCE_DESC="$("${AWS[@]}" ec2 describe-vpc-endpoints \
      --filters "Name=vpc-id,Values=${VPC_ID}" \
      2>/dev/null)" || VPCE_DESC=""

  if [ -z "${VPCE_DESC}" ]; then
    note "describe-vpc-endpoints returned nothing (permission denied or no endpoints)."
  else
    printf '%s' "${VPCE_DESC}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
endpoints = d.get('VpcEndpoints', [])
if not endpoints:
    print('  (no VPC endpoints in this VPC)')
    print('  ** If the dependency is an AWS service and this subnet has no NAT, traffic cannot reach it. **')
else:
    print('  %-40s  %-10s  %-12s  %s' % ('Service', 'Type', 'State', 'Endpoint ID'))
    print('  ' + '-'*90)
    for ep in endpoints:
        svc   = ep.get('ServiceName', '?').split('.')[-1]   # e.g. 'secretsmanager'
        etype = ep.get('VpcEndpointType', '?')
        state = ep.get('State', '?')
        eid   = ep.get('VpcEndpointId', '?')
        print('  %-40s  %-10s  %-12s  %s' % (svc, etype, state, eid))
    print()
    print('  Common AWS service endpoints needed for ECS Fargate in a private subnet:')
    needed = ['ecr.api', 'ecr.dkr', 'secretsmanager', 'ssm', 'ssmmessages',
              'ec2messages', 'logs', 'sts']
    present = set(ep.get('ServiceName', '').split('.')[-1] for ep in endpoints)
    for svc in needed:
        status = 'present' if svc in present else 'MISSING'
        print('    %-20s  %s' % (svc, status))
" 2>/dev/null || note "Could not parse VPC endpoints output."
  fi
fi

echo
echo "Done. Findings are hypotheses — a human can confirm with VPC Reachability Analyzer (ec2 start-network-insights-analysis)."
