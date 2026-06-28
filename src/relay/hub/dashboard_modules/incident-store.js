// Canonical client-side incident store — single source of truth for the
// incident list on the client.  Both the open-tab and history-tab are pure
// filtered views of the data held here; they never fetch independently.
//
// Public API:
//   refresh()          — async; fetches /incidents + /incidents/history and
//                        populates the store.  Keeps last-known data on error.
//   getOpen()          — array of open incidents (TRIGGERED / ACKNOWLEDGED /
//                        ESCALATED), sorted newest-first by created_at.
//   getHistory()       — array of terminal incidents (RESOLVED / CLOSED),
//                        sorted newest-first by created_at.
//   subscribe(fn)      — register a zero-arg callback invoked after every
//                        successful refresh.  Returns an unsubscribe function.
//
// The store is intentionally dependency-light: no framework, no state.js
// coupling.  It is the cross-module data layer; display logic stays in the
// view modules that import it.

const OPEN_STATES = new Set(['TRIGGERED', 'ACKNOWLEDGED', 'ESCALATED']);
const TERMINAL_STATES = new Set(['RESOLVED', 'CLOSED']);

/** @type {Map<string, object>} keyed by correlation_id */
const _incidents = new Map();

/** @type {Array<() => void>} */
const _listeners = [];

function _notify() {
  for (const fn of _listeners) {
    try { fn(); } catch (_) {}
  }
}

/**
 * Fetch both /incidents and /incidents/history and merge into the store.
 * On network / parse failure the store retains its last-known data and
 * _notify() is NOT called (no spurious re-render from stale data).
 *
 * @returns {Promise<void>}
 */
export async function refresh() {
  let open = [], history = [];
  try {
    const [rOpen, rHist] = await Promise.all([
      fetch('/incidents'),
      fetch('/incidents/history'),
    ]);
    if (!rOpen.ok || !rHist.ok) return;   // keep last-known data silently
    [open, history] = await Promise.all([rOpen.json(), rHist.json()]);
  } catch (_) {
    return;   // network failure — keep last-known data
  }
  _incidents.clear();
  for (const inc of open)    _incidents.set(inc.correlation_id, inc);
  for (const inc of history) _incidents.set(inc.correlation_id, inc);
  _notify();
}

/**
 * Return open incidents sorted newest-first.
 * @returns {object[]}
 */
export function getOpen() {
  return [..._incidents.values()]
    .filter(i => OPEN_STATES.has(i.state))
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

/**
 * Return terminal (resolved/closed) incidents sorted newest-first.
 * @returns {object[]}
 */
export function getHistory() {
  return [..._incidents.values()]
    .filter(i => TERMINAL_STATES.has(i.state))
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

/**
 * Subscribe to store updates.  The callback fires after every successful
 * refresh().  Returns an unsubscribe function.
 *
 * @param {() => void} fn
 * @returns {() => void} unsubscribe
 */
export function subscribe(fn) {
  _listeners.push(fn);
  return () => {
    const idx = _listeners.indexOf(fn);
    if (idx !== -1) _listeners.splice(idx, 1);
  };
}
