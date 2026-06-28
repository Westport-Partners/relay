// Client-side KPI recompute — a faithful port of core/metrics.py::compute_metrics.
// This is the ONE place the client mirrors the server KPI math; the parity test
// (tests/test_metrics_parity.py) pins it to core/metrics.py. When either side
// changes, keep that test green.
//
// Input: an array of incident objects as serialized by /incidents +
// /incidents/history (enriched with acknowledged_at, resolved_at, signal_source,
// synthetic). Output: a dict equal to IncidentMetrics.as_dict() for the same set.

const SEVERITIES = ['SEV1', 'SEV2', 'SEV3', 'SEV4'];
const TERMINAL_STATES = new Set(['RESOLVED', 'CLOSED']);

// Round half-to-even? No — Python's round() is banker's rounding, but
// core/metrics.py rounds to 1 decimal where ties are vanishingly unlikely on
// real second-granularity durations. Match Python's round() semantics (round
// half to even) so the parity test is exact.
function round1(x) {
  if (x === null || x === undefined) return null;
  const scaled = x * 10;
  const floor = Math.floor(scaled);
  const diff = scaled - floor;
  let r;
  if (diff > 0.5) r = floor + 1;
  else if (diff < 0.5) r = floor;
  else r = (floor % 2 === 0) ? floor : floor + 1;  // half-to-even
  return r / 10;
}

// (b - a) in seconds, or null if either missing or b < a. Inputs are ISO strings.
function seconds(aIso, bIso) {
  if (!aIso || !bIso) return null;
  const a = Date.parse(aIso);
  const b = Date.parse(bIso);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  const delta = (b - a) / 1000;
  return delta >= 0 ? delta : null;
}

// Linear-interpolated percentile of an already-sorted array. Mirrors
// core/metrics._percentile exactly.
function percentile(sortedVals, q) {
  if (!sortedVals.length) return 0.0;
  if (sortedVals.length === 1) return sortedVals[0];
  const pos = q * (sortedVals.length - 1);
  const lo = Math.floor(pos);
  const frac = pos - lo;
  if (lo + 1 < sortedVals.length) {
    return sortedVals[lo] + frac * (sortedVals[lo + 1] - sortedVals[lo]);
  }
  return sortedVals[lo];
}

// Summary of a set of durations (seconds) — mirrors DurationStat.from_samples.
function durationStat(samples) {
  const vals = samples.filter(s => s !== null && s !== undefined).sort((a, b) => a - b);
  if (!vals.length) {
    return { count: 0, mean: null, p50: null, p90: null, max: null };
  }
  const sum = vals.reduce((acc, v) => acc + v, 0);
  return {
    count: vals.length,
    mean: round1(sum / vals.length),
    p50: round1(percentile(vals, 0.50)),
    p90: round1(percentile(vals, 0.90)),
    max: round1(vals[vals.length - 1]),
  };
}

// Compute KPIs over `incidents`. Synthetic incidents ARE counted in every
// number (matches the server); synthetic_total reports how many.
export function computeMetrics(incidents) {
  const bySeverity = {};
  for (const s of SEVERITIES) bySeverity[s] = 0;  // stable SEV1-4 axis
  const bySource = {};
  const byState = {};
  const ackSamples = [];
  const resolveSamples = [];

  let open = 0;
  let resolved = 0;
  let acknowledged = 0;
  let syntheticTotal = 0;

  for (const inc of incidents) {
    if (inc.synthetic) syntheticTotal += 1;

    const sev = inc.severity;
    bySeverity[sev] = (bySeverity[sev] || 0) + 1;
    const src = inc.signal_source;
    bySource[src] = (bySource[src] || 0) + 1;
    const st = inc.state;
    byState[st] = (byState[st] || 0) + 1;

    if (TERMINAL_STATES.has(inc.state)) resolved += 1;
    else open += 1;

    if (inc.acknowledged_at) {
      acknowledged += 1;
      const tta = seconds(inc.created_at, inc.acknowledged_at);
      if (tta !== null) ackSamples.push(tta);
    }

    if (inc.resolved_at) {
      const ttr = seconds(inc.created_at, inc.resolved_at);
      if (ttr !== null) resolveSamples.push(ttr);
    }
  }

  return {
    total: incidents.length,
    synthetic_total: syntheticTotal,
    open,
    resolved,
    acknowledged,
    by_severity: bySeverity,
    by_source: bySource,
    by_state: byState,
    time_to_ack_seconds: durationStat(ackSamples),
    time_to_resolve_seconds: durationStat(resolveSamples),
  };
}
