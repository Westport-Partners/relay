#!/usr/bin/env bash
# lambda-errors/probe.sh — READ-ONLY Lambda function diagnostics.
#
# Uses only get*/list*/describe*/filter-log-events/get-metric-statistics calls.
# Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION          required  AWS region
#   RELAY_APP_NAME        required  app name (used to discover the function)
#   RELAY_FUNCTION_NAME   optional  Lambda function name or ARN (else discovered)
#   RELAY_WINDOW_MINUTES  optional  lookback for metrics and logs (default 60)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
APP="${RELAY_APP_NAME:-}"
FUNC="${RELAY_FUNCTION_NAME:-}"
WINDOW="${RELAY_WINDOW_MINUTES:-60}"

if [ -z "${REGION}" ] || [ -z "${APP}" ]; then
  echo "ERROR: RELAY_REGION and RELAY_APP_NAME are required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Resolve function name
# ---------------------------------------------------------------------------
section "Resolution"
if [ -z "${FUNC}" ]; then
  note "RELAY_FUNCTION_NAME not set; discovering by app name '${APP}'."
  RAW_LIST="$("${AWS[@]}" lambda list-functions --query 'Functions[].FunctionName' 2>/dev/null || true)"
  if [ -z "${RAW_LIST}" ]; then
    note "list-functions returned nothing (no functions or permission denied). Stopping."
    exit 0
  fi
  FUNC="$(printf '%s' "${RAW_LIST}" | python3 -c "
import sys, json
names = json.load(sys.stdin)
app = '${APP}'.lower()
# prefer exact substring match
for n in names:
    if app in n.lower():
        print(n)
        sys.exit(0)
# fall back to first
if names:
    print(names[0])
    import sys; sys.stderr.write('[note] no name match; using first function: ' + names[0] + '\n')
" 2>/dev/null || true)"
fi

if [ -z "${FUNC}" ]; then
  note "No Lambda function found matching '${APP}'. Stopping."
  exit 0
fi
echo "  Function: ${FUNC}"

# ---------------------------------------------------------------------------
# 2. Function configuration
# ---------------------------------------------------------------------------
section "Function config"
CFG="$("${AWS[@]}" lambda get-function-configuration --function-name "${FUNC}" 2>/dev/null || true)"
if [ -z "${CFG}" ]; then
  note "get-function-configuration failed (permission denied or function not found)."
else
  printf '%s' "${CFG}" | python3 -c "
import sys, json
d = json.load(sys.stdin)

print('  Runtime      :', d.get('Runtime'))
print('  MemorySize   :', d.get('MemorySize'), 'MB')
print('  Timeout      :', d.get('Timeout'), 's')
print('  LastModified :', d.get('LastModified'))
print('  State        :', d.get('State'))

state_reason = d.get('StateReason')
if state_reason:
    print('  StateReason  :', state_reason)

last_update = d.get('LastUpdateStatus')
print('  LastUpdateStatus :', last_update)

update_reason = d.get('LastUpdateStatusReason')
if update_reason:
    print('  LastUpdateStatusReason :', update_reason)

reserved = d.get('ReservedConcurrentExecutions')
if reserved is not None:
    print('  ReservedConcurrentExecutions :', reserved)
else:
    print('  ReservedConcurrentExecutions : (not set — uses unreserved pool)')

# Print env var KEYS only — never values (may hold secrets)
env_vars = d.get('Environment', {}).get('Variables', {})
if env_vars:
    print('  Env var keys :', ', '.join(sorted(env_vars.keys())))
else:
    print('  Env var keys : (none)')

# Flag unhealthy states
if d.get('State') not in (None, 'Active'):
    print('  *** FLAG: State is', d.get('State'), '— function may be unavailable ***')
if last_update not in (None, 'Successful'):
    print('  *** FLAG: LastUpdateStatus is', last_update, '— last deploy/config update may have failed ***')
" 2>/dev/null || note "could not parse function configuration."
fi

# ---------------------------------------------------------------------------
# 3. Invocation metrics
# ---------------------------------------------------------------------------
section "Invocation metrics (last ${WINDOW}m)"

START_TIME="$(date -u -d "-${WINDOW} minutes" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
  || python3 -c "from datetime import datetime, timedelta; print((datetime.utcnow()-timedelta(minutes=${WINDOW})).strftime('%Y-%m-%dT%H:%M:%SZ'))")"
END_TIME="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

get_metric() {
  local metric_name="$1"
  local stat="$2"
  "${AWS[@]}" cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name "${metric_name}" \
    --dimensions Name=FunctionName,Value="${FUNC}" \
    --start-time "${START_TIME}" \
    --end-time "${END_TIME}" \
    --period 60 \
    --statistics "${stat}" \
    2>/dev/null || true
}

ERRORS_RAW="$(get_metric Errors Sum)"
THROTTLES_RAW="$(get_metric Throttles Sum)"
INVOCATIONS_RAW="$(get_metric Invocations Sum)"
DURATION_MAX_RAW="$(get_metric Duration Maximum)"
DURATION_AVG_RAW="$(get_metric Duration Average)"
CONCURRENT_RAW="$(get_metric ConcurrentExecutions Maximum)"

python3 -c "
import sys, json

def peak_sum(raw):
    try:
        pts = json.loads(raw).get('Datapoints', [])
        if not pts: return None
        return sum(p.get('Sum', 0) for p in pts)
    except Exception:
        return None

def peak_max(raw):
    try:
        pts = json.loads(raw).get('Datapoints', [])
        if not pts: return None
        return max(p.get('Maximum', 0) for p in pts)
    except Exception:
        return None

def peak_avg(raw):
    try:
        pts = json.loads(raw).get('Datapoints', [])
        if not pts: return None
        avgs = [p.get('Average', 0) for p in pts]
        return sum(avgs) / len(avgs)
    except Exception:
        return None

errors      = peak_sum('''${ERRORS_RAW}''')
throttles   = peak_sum('''${THROTTLES_RAW}''')
invocations = peak_sum('''${INVOCATIONS_RAW}''')
dur_max     = peak_max('''${DURATION_MAX_RAW}''')
dur_avg     = peak_avg('''${DURATION_AVG_RAW}''')
concurrent  = peak_max('''${CONCURRENT_RAW}''')

def fmt(v, unit=''):
    if v is None: return '(no data)'
    return str(round(v, 2)) + (' ' + unit if unit else '')

print('  Errors (sum)            :', fmt(errors))
print('  Throttles (sum)         :', fmt(throttles))
print('  Invocations (sum)       :', fmt(invocations))
print('  Duration maximum        :', fmt(dur_max, 'ms'))
print('  Duration average        :', fmt(dur_avg, 'ms'))
print('  ConcurrentExecutions max:', fmt(concurrent))

if invocations and invocations > 0 and errors is not None:
    rate = round(100.0 * errors / invocations, 2)
    print('  Error rate              :', rate, '%')
else:
    print('  Error rate              : (cannot compute — invocations=0 or no data)')
" 2>/dev/null || note "could not compute invocation metrics."

# ---------------------------------------------------------------------------
# 4. Concurrency headroom
# ---------------------------------------------------------------------------
section "Concurrency headroom"
ACCT_SETTINGS="$("${AWS[@]}" lambda get-account-settings 2>/dev/null || true)"
if [ -z "${ACCT_SETTINGS}" ]; then
  note "get-account-settings failed (permission denied?)."
else
  # Re-read reserved concurrency to avoid dependency on CFG being set
  RESERVED_RAW="$(printf '%s' "${CFG:-{}}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
v = d.get('ReservedConcurrentExecutions')
print(v if v is not None else 'null')
" 2>/dev/null || echo "null")"

  CONCURRENT_PEAK="$(python3 -c "
import json
try:
    pts = json.loads('''${CONCURRENT_RAW}''').get('Datapoints', [])
    peak = max((p.get('Maximum', 0) for p in pts), default=None)
    print(round(peak, 0) if peak is not None else 'null')
except Exception:
    print('null')
" 2>/dev/null || echo "null")"

  printf '%s' "${ACCT_SETTINGS}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
lim = d.get('AccountLimit', {})
account_limit      = lim.get('ConcurrentExecutions')
unreserved_limit   = lim.get('UnreservedConcurrentExecutions')
reserved_func      = ${RESERVED_RAW}
peak_observed      = ${CONCURRENT_PEAK}

print('  Account concurrent limit    :', account_limit)
print('  Unreserved pool             :', unreserved_limit)
print('  Function reserved concurrency:', reserved_func if reserved_func is not None else '(not set)')
print('  Observed peak concurrency   :', peak_observed if peak_observed is not None else '(no data)')

# Determine effective cap for this function
effective_cap = reserved_func if reserved_func is not None else unreserved_limit

if effective_cap is not None and peak_observed is not None:
    headroom = effective_cap - peak_observed
    print('  Headroom                    :', round(headroom, 0), 'below effective cap')
    if headroom <= 0:
        print('  *** FLAG: peak concurrency reached/exceeded effective cap — throttling likely ***')
    elif headroom < 0.1 * effective_cap:
        print('  *** FLAG: headroom <10% of effective cap — throttling risk ***')
" 2>/dev/null || note "could not parse account settings."
fi

# ---------------------------------------------------------------------------
# 5. Recent error logs
# ---------------------------------------------------------------------------
section "Recent error logs (last ${WINDOW}m, up to 20 lines)"
LOG_GROUP="/aws/lambda/${FUNC}"
FILTER_PATTERN='?ERROR ?\"Task timed out\" ?\"Runtime.ExitError\" ?Unhandled ?Exception ?errorMessage'

LOG_RAW="$("${AWS[@]}" logs filter-log-events \
  --log-group-name "${LOG_GROUP}" \
  --start-time "$(python3 -c "
import time, sys
from datetime import datetime, timedelta
dt = datetime.utcnow() - timedelta(minutes=${WINDOW})
print(int(dt.timestamp() * 1000))
")" \
  --filter-pattern "${FILTER_PATTERN}" \
  --limit 20 \
  2>/dev/null || true)"

if [ -z "${LOG_RAW}" ]; then
  note "filter-log-events returned nothing (log group '${LOG_GROUP}' may not exist, or no matches, or permission denied)."
else
  printf '%s' "${LOG_RAW}" | python3 -c "
import sys, json
from datetime import datetime, timezone

d = json.load(sys.stdin)
events = d.get('events', [])
if not events:
    print('  (no matching log events in window)')
else:
    for e in events:
        ts_ms = e.get('timestamp', 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        msg = e.get('message', '').rstrip()
        print('  [' + ts + ']', msg)
    print()
    print('  Total matching events shown:', len(events))
" 2>/dev/null || note "could not parse log events."
fi

echo
echo "Done. Findings are hypotheses — correlate deploy times with the alarm (see recent-changes)."
