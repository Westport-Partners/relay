// Status taxonomy shared across the fleet board and tile drawer.
// Ported verbatim from dashboard_parts/20-shared-helpers.js.part (#33).

export const STATUS_ORDER = { red: 0, degraded: 1, grey: 2, green: 3 };

export const MARKER = { red: '!', degraded: '▲', grey: '?', green: '✓' };

export const STATUS_LABEL = {
  red:      t => t.liveness === 'lost' ? 'NO SIGNAL' : (t.worst_severity || 'RED'),
  degraded: t => t.liveness === 'stale' ? 'STALE' : (t.worst_severity || 'DEGRADED'),
  grey:     () => 'UNKNOWN',
  green:    () => 'OK',
};
