// Global environment lens — the one place that owns the env match predicate,
// the persisted selection, and the re-render fan-out. Environment is the
// namespace ABOVE the org hierarchy; this is a VIEW filter over one Hub's
// already-scoped data, never a security boundary.

import { tiles, activeEnv, activeView, setActiveEnv } from './state.js';
import { envsPresent } from './fleet-groups.js';
import { renderAll } from './fleet.js';
import { renderIncidentsFromStore } from './incidents.js';
import { loadMetrics } from './metrics.js';

const STORAGE_KEY = 'relay.env-filter';

// Read the persisted selection. At call time tiles may be empty (SSE snapshot
// not yet arrived), so we cannot validate against the live env set here.
// Instead we return the raw stored string when it looks plausible ('all' or a
// non-empty string), and let buildEnvFilter() reconcile once tiles are loaded.
// Missing, empty, or unreadable (private-mode) storage returns 'all' — never
// throws so a corrupt value can't blank the dashboard (FR-010, C8).
export function readPersistedEnv() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return (v && typeof v === 'string') ? v : 'all';
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
    default:          renderAll(); break;
  }
}

// Build (or rebuild) the env filter buttons from the live tile set.
// Safe to call repeatedly — clears and repopulates the container each time.
// Called once at init (renders just ALL before tiles arrive) and again after
// each SSE snapshot/delta so newly-seen environments appear immediately.
export function buildEnvFilter() {
  const container = document.getElementById('env-filter');
  if (!container) return;

  const present = envsPresent(Array.from(tiles.values()));

  // Reconcile persisted selection: if the stored env is no longer present in
  // the live tile set (e.g. an environment was removed or the user moved to a
  // different Hub), reset to 'all' so the board never shows a blank result.
  // We skip this check when tiles is still empty (first call before snapshot).
  if (tiles.size > 0 && activeEnv !== 'all' && !present.includes(activeEnv)) {
    setActiveEnv('all');
    persistEnv('all');
  }

  // Remove existing buttons before repopulating.
  container.querySelectorAll('.filter-btn').forEach(b => b.remove());

  // Build ALL button first.
  const mkBtn = (envVal, label) => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn';
    btn.dataset.env = envVal;
    btn.textContent = label;
    if (envVal === activeEnv) btn.classList.add('active');
    btn.onclick = () => {
      container.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      setActiveEnv(btn.dataset.env);
      persistEnv(btn.dataset.env);
      applyEnvToAll();
    };
    container.appendChild(btn);
  };

  mkBtn('all', 'All');
  for (const env of present) {
    mkBtn(env.toLowerCase(), env);
  }
}
