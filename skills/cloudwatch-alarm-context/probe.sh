#!/usr/bin/env bash
# cloudwatch-alarm-context/probe.sh — READ-ONLY CloudWatch alarm diagnostics.
#
# Uses only describe*/get*/list*/filter-log-events calls. Never mutates account state.
#
# Inputs (environment):
#   RELAY_REGION          required  AWS region
#   RELAY_APP_NAME        required  app name (used to match alarms/log groups)
#   RELAY_ALARM_NAME      optional  exact firing alarm name (else discovered by app name)
#   RELAY_LOG_GROUP       optional  CloudWatch Logs group to tail (else discovered)
#   RELAY_WINDOW_MINUTES  optional  lookback window in minutes (default 60)
#
# Prints human-readable sections; each section is isolated so one failure
# (missing permission, not-found) prints a note and the probe continues.
set -uo pipefail

REGION="${RELAY_REGION:-}"
APP="${RELAY_APP_NAME:-}"
ALARM_NAME="${RELAY_ALARM_NAME:-}"
LOG_GROUP="${RELAY_LOG_GROUP:-}"
WINDOW="${RELAY_WINDOW_MINUTES:-60}"

if [ -z "${REGION}" ] || [ -z "${APP}" ]; then
  echo "ERROR: RELAY_REGION and RELAY_APP_NAME are required." >&2
  exit 2
fi

AWS=(aws --region "${REGION}" --output json)

section() { printf '\n=== %s ===\n' "$1"; }
note()    { printf '  [note] %s\n' "$1"; }

# Compute ISO-8601 start time (window minutes ago) — no jq, pure python3.
START_TIME="$(python3 -c "
from datetime import datetime, timezone, timedelta
t = datetime.now(timezone.utc) - timedelta(minutes=${WINDOW})
print(t.strftime('%Y-%m-%dT%H:%M:%SZ'))
" 2>/dev/null)"
END_TIME="$(python3 -c "
from datetime import datetime, timezone
print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
" 2>/dev/null)"

# ---------------------------------------------------------------------------
# 1. Resolution — choose alarm and log group, report how
# ---------------------------------------------------------------------------
section "Resolution"
echo "  App        : ${APP}"
echo "  Window     : ${WINDOW}m  (${START_TIME} → ${END_TIME})"

# Resolve alarm name
if [ -z "${ALARM_NAME}" ]; then
  note "RELAY_ALARM_NAME not set; querying alarms in ALARM state and matching on '${APP}'."
  ALARMS_RAW="$("${AWS[@]}" cloudwatch describe-alarms --state-value ALARM 2>/dev/null)" || ALARMS_RAW=""
  if [ -n "${ALARMS_RAW}" ]; then
    ALARM_NAME="$(printf '%s' "${ALARMS_RAW}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
app = '${APP}'.lower()
for a in d.get('MetricAlarms', []):
    if app in a.get('AlarmName', '').lower():
        print(a['AlarmName'])
        break
" 2>/dev/null)"
  fi
  if [ -z "${ALARM_NAME}" ]; then
    note "No alarm matched '${APP}' in ALARM state. Sections 2 and 3 will be skipped."
  else
    echo "  Alarm      : ${ALARM_NAME}  (discovered by app-name match)"
  fi
else
  echo "  Alarm      : ${ALARM_NAME}  (from RELAY_ALARM_NAME)"
fi

# Resolve log group
if [ -z "${LOG_GROUP}" ]; then
  note "RELAY_LOG_GROUP not set; discovering log groups matching '${APP}'."
  LG_RAW="$("${AWS[@]}" logs describe-log-groups \
      --log-group-name-prefix "/aws" 2>/dev/null)" || LG_RAW=""
  if [ -n "${LG_RAW}" ]; then
    LOG_GROUP="$(printf '%s' "${LG_RAW}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
app = '${APP}'.lower()
for g in d.get('logGroups', []):
    if app in g.get('logGroupName', '').lower():
        print(g['logGroupName'])
        break
" 2>/dev/null)"
  fi
  if [ -z "${LOG_GROUP}" ]; then
    # Try broader prefix
    LG_RAW2="$("${AWS[@]}" logs describe-log-groups 2>/dev/null)" || LG_RAW2=""
    if [ -n "${LG_RAW2}" ]; then
      LOG_GROUP="$(printf '%s' "${LG_RAW2}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
app = '${APP}'.lower()
for g in d.get('logGroups', []):
    if app in g.get('logGroupName', '').lower():
        print(g['logGroupName'])
        break
" 2>/dev/null)"
    fi
  fi
  if [ -z "${LOG_GROUP}" ]; then
    note "No log group matched '${APP}'. Section 5 (error logs) will be skipped."
  else
    echo "  Log group  : ${LOG_GROUP}  (discovered by app-name match)"
  fi
else
  echo "  Log group  : ${LOG_GROUP}  (from RELAY_LOG_GROUP)"
fi

# ---------------------------------------------------------------------------
# 2. Alarm detail — metric, threshold, current StateReason
# ---------------------------------------------------------------------------
section "Alarm detail"
if [ -z "${ALARM_NAME}" ]; then
  note "No alarm name resolved; skipping alarm detail."
else
  ALARM_RAW="$("${AWS[@]}" cloudwatch describe-alarms \
      --alarm-names "${ALARM_NAME}" 2>/dev/null)" || ALARM_RAW=""
  if [ -z "${ALARM_RAW}" ]; then
    note "describe-alarms returned nothing (permission or not found)."
  else
    printf '%s' "${ALARM_RAW}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
alarms = d.get('MetricAlarms', [])
if not alarms:
    print('  [note] No MetricAlarms in response (may be composite alarm).')
    sys.exit(0)
a = alarms[0]
print('  AlarmName      :', a.get('AlarmName'))
print('  State          :', a.get('StateValue'))
print('  Namespace      :', a.get('Namespace'))
print('  MetricName     :', a.get('MetricName'))
print('  Statistic      :', a.get('Statistic', a.get('ExtendedStatistic', '?')))
print('  Period (s)     :', a.get('Period'))
print('  EvalPeriods    :', a.get('EvaluationPeriods'))
print('  Threshold      :', a.get('Threshold'))
print('  ComparisonOp   :', a.get('ComparisonOperator'))
dims = a.get('Dimensions', [])
if dims:
    print('  Dimensions     :', ', '.join('%s=%s' % (x['Name'], x['Value']) for x in dims))
print('  StateReason    :', a.get('StateReason', '')[:400])
" 2>/dev/null || note "could not parse alarm detail."
  fi
fi

# ---------------------------------------------------------------------------
# 3. Metric history — datapoints over the window, sorted by time
# ---------------------------------------------------------------------------
section "Metric history (last ${WINDOW}m, 60s resolution)"
if [ -z "${ALARM_NAME}" ] || [ -z "${ALARM_RAW:-}" ]; then
  note "No alarm detail available; skipping metric history."
else
  # Extract metric params from the cached alarm description
  METRIC_PARAMS="$(printf '%s' "${ALARM_RAW}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
alarms = d.get('MetricAlarms', [])
if not alarms:
    sys.exit(1)
a = alarms[0]
ns = a.get('Namespace', '')
metric = a.get('MetricName', '')
stat = a.get('Statistic', 'Average')
dims = a.get('Dimensions', [])
dim_str = ' '.join('Name=%s,Value=%s' % (x['Name'], x['Value']) for x in dims)
print(ns)
print(metric)
print(stat)
print(dim_str)
" 2>/dev/null)"

  if [ -z "${METRIC_PARAMS}" ]; then
    note "Could not extract metric parameters from alarm; skipping metric history."
  else
    METRIC_NS="$(printf '%s' "${METRIC_PARAMS}" | sed -n '1p')"
    METRIC_NAME="$(printf '%s' "${METRIC_PARAMS}" | sed -n '2p')"
    METRIC_STAT="$(printf '%s' "${METRIC_PARAMS}" | sed -n '3p')"
    METRIC_DIMS="$(printf '%s' "${METRIC_PARAMS}" | sed -n '4p')"

    if [ -n "${METRIC_DIMS}" ]; then
      STATS_RAW="$("${AWS[@]}" cloudwatch get-metric-statistics \
          --namespace "${METRIC_NS}" \
          --metric-name "${METRIC_NAME}" \
          --dimensions ${METRIC_DIMS} \
          --start-time "${START_TIME}" \
          --end-time "${END_TIME}" \
          --period 60 \
          --statistics "${METRIC_STAT}" 2>/dev/null)" || STATS_RAW=""
    else
      STATS_RAW="$("${AWS[@]}" cloudwatch get-metric-statistics \
          --namespace "${METRIC_NS}" \
          --metric-name "${METRIC_NAME}" \
          --start-time "${START_TIME}" \
          --end-time "${END_TIME}" \
          --period 60 \
          --statistics "${METRIC_STAT}" 2>/dev/null)" || STATS_RAW=""
    fi

    if [ -z "${STATS_RAW}" ]; then
      note "get-metric-statistics returned nothing (permission or no data)."
    else
      printf '%s' "${STATS_RAW}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
pts = sorted(d.get('Datapoints', []), key=lambda x: x.get('Timestamp', ''))
label = d.get('Label', 'metric')
stat_key = None
for k in ('Average','Sum','Maximum','Minimum','SampleCount'):
    if pts and k in pts[0]:
        stat_key = k
        break
if not pts:
    print('  (no datapoints in window)')
else:
    print('  %-28s  %s' % ('Timestamp (UTC)', label))
    print('  ' + '-'*50)
    for p in pts:
        val = p.get(stat_key, '?') if stat_key else str(p)
        print('  %-28s  %s %s' % (p.get('Timestamp','?'), val, p.get('Unit','')))
" 2>/dev/null || note "could not parse metric datapoints."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 4. Sibling alarms in ALARM (blast radius)
# ---------------------------------------------------------------------------
section "Sibling alarms currently in ALARM (blast radius)"
SIBLING_RAW="$("${AWS[@]}" cloudwatch describe-alarms --state-value ALARM 2>/dev/null)" || SIBLING_RAW=""
if [ -z "${SIBLING_RAW}" ]; then
  note "describe-alarms --state-value ALARM failed or returned nothing."
else
  printf '%s' "${SIBLING_RAW}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
alarms = d.get('MetricAlarms', []) + d.get('CompositeAlarms', [])
if not alarms:
    print('  (no alarms currently in ALARM state)')
else:
    print('  %-50s  %-30s  %s' % ('AlarmName', 'MetricName', 'Namespace'))
    print('  ' + '-'*90)
    for a in alarms:
        print('  %-50s  %-30s  %s' % (
            a.get('AlarmName', '')[:50],
            a.get('MetricName', a.get('AlarmRule', '(composite)'))[:30],
            a.get('Namespace', '')[:30]))
" 2>/dev/null || note "could not parse sibling alarms."
fi

# ---------------------------------------------------------------------------
# 5. Recent error log lines
# ---------------------------------------------------------------------------
section "Recent error logs (last ${WINDOW}m)"
if [ -z "${LOG_GROUP}" ]; then
  note "No log group resolved; skipping error log search."
else
  FILTER_PATTERN='?ERROR ?Error ?error ?Exception ?timeout ?5xx'
  note "Searching '${LOG_GROUP}' with pattern: ${FILTER_PATTERN}"
  LOGS_RAW="$("${AWS[@]}" logs filter-log-events \
      --log-group-name "${LOG_GROUP}" \
      --start-time "$(python3 -c "
import time
from datetime import datetime, timezone, timedelta
t = datetime.now(timezone.utc) - timedelta(minutes=${WINDOW})
print(int(t.timestamp() * 1000))
")" \
      --filter-pattern "${FILTER_PATTERN}" \
      --limit 20 2>/dev/null)" || LOGS_RAW=""

  if [ -z "${LOGS_RAW}" ]; then
    note "filter-log-events returned nothing (permission, no log group, or no matches)."
  else
    printf '%s' "${LOGS_RAW}" | python3 -c "
import sys, json
from datetime import datetime, timezone
d = json.load(sys.stdin)
events = d.get('events', [])
if not events:
    print('  (no error-pattern matches in window)')
else:
    print('  %d event(s) matched:' % len(events))
    for e in events[-20:]:
        ts_ms = e.get('timestamp', 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        msg = e.get('message', '').rstrip()[:200]
        print('  [%s] %s' % (ts, msg))
" 2>/dev/null || note "could not parse log events."
  fi
fi

echo
echo "Done. Findings are hypotheses — correlate with sibling alarms and recent-changes."
