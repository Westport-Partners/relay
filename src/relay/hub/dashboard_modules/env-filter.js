// Global environment lens — the one place that owns the env match predicate,
// the persisted selection, and the re-render fan-out. Environment is the
// namespace ABOVE the org hierarchy; this is a VIEW filter over one Hub's
// already-scoped data, never a security boundary.

import { activeEnv, activeView } from './state.js';
import { renderAll } from './fleet.js';
import { renderIncidentsFromStore } from './incidents.js';
import { loadMetrics } from './metrics.js';

// Fixed v1 choice set. ALL means "no env filtering". A later phase may derive
// the specific envs from the live fleet (US3 future) — keep this list as the
// single source so that change is small.
export const ENV_CHOICES = ['all', 'prod', 'test', 'dev'];

const STORAGE_KEY = 'relay.env-filter';

// Read the persisted selection, validated against the known set. Missing,
// unknown, or unreadable (private-mode) storage falls back to 'all' — never
// throws, so a corrupt value can't blank the dashboard (FR-010, C8).
export function readPersistedEnv() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return ENV_CHOICES.includes(v) ? v : 'all';
  } catch (_) {
    return 'all';
  }
}

// Persist the selection. Best-effort: storage failures are swallowed so the
// in-memory lens still works.
export function persistEnv(env) {
  try {
    localStorage.setItem(STORAGE_KEY, env);
  } catch (_) {
    // ignore — private mode / disabled storage
  }
}

// Does an item belong to the active environment lens?
//   - 'all'                → everything matches
//   - a specific env       → case-insensitive equality on item.environment
//   - empty/absent env     → matches only under 'all'
export function matchesEnv(item, env = activeEnv) {
  if (env === 'all') return true;
  return String((item && item.environment) || '').toLowerCase() === env;
}

// Re-render the currently-active view so the new env lens takes effect with no
// network round-trip on the client filter path (Metrics re-fetches as before).
// Each view reads activeEnv at render time, so we only need to nudge the one
// on screen; the others re-apply lazily when navigated to.
export function applyEnvToAll() {
  switch (activeView) {
    case 'fleet':     renderAll(); break;
    case 'incidents': renderIncidentsFromStore(); break;
    case 'metrics':   loadMetrics(); break;
  }
}
