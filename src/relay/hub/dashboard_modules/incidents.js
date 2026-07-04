// Incidents list view — open and history tabs rendered as filtered views of
// the shared incident store (incident-store.js).  No direct fetching here.
// Ported from dashboard_parts/23-view-incidents.js.part (#33).

import { esc, fmtAge, abbrAccount } from './helpers.js';
import { openIncident } from './incident-drawer.js';
import { refresh, getOpen, getHistory, subscribe } from './incident-store.js';
import { matchesEnv } from './env-filter.js';

// Module-local tab state (single writer, never read across modules).
let incidentsTab = 'open'; // 'open' | 'history'

// Unsubscribe handle — replaced each time loadIncidents() sets up the
// subscription so we never stack duplicate listeners across nav round-trips.
let _unsubscribe = null;

/** Render the currently-active tab from store data (no fetch). */
function _renderFromStore() {
  const list  = document.getElementById('incidents-list');
  const empty = document.getElementById('incidents-empty');
  if (!list || !empty) return;   // view not mounted yet

  const data = (incidentsTab === 'history' ? getHistory() : getOpen())
    .filter(i => matchesEnv(i));

  if (!data.length) {
    list.innerHTML = '';
    empty.style.display = 'block';
    empty.textContent = incidentsTab === 'history' ? 'No incident history.' : 'No open incidents.';
    return;
  }
  empty.style.display = 'none';
  list.innerHTML = '';
  for (const i of data) {
    const row = document.createElement('div');
    // Tint + left-border active rows by urgency: red for SEV1/2, amber for SEV3.
    const active = i.state === 'TRIGGERED' || i.state === 'ESCALATED';
    const sevCls = (i.severity === 'SEV1' || i.severity === 'SEV2') ? 'inc-row-red'
                 : (i.severity === 'SEV3') ? 'inc-row-amber' : '';
    row.className = 'inc-row' + (active && sevCls ? ' ' + sevCls : '');
    row.addEventListener('click', () => openIncident(i.correlation_id));
    row.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:3px;align-items:flex-start;">
        <span class="inc-sev ${esc(i.severity)}">${esc(i.severity || '—')}</span>
        ${i.synthetic ? '<span class="badge-synthetic">TEST</span>' : ''}
      </div>
      <div>
        <div class="inc-app">${esc(i.app_name)}</div>
        <div class="inc-sub">${esc(i.environment)} · ${esc(i.alarm_name || '')}</div>
      </div>
      <span class="inc-state">${esc(i.state || '')}</span>
      <span class="inc-sub">${esc(abbrAccount(i.account_id || ''))}</span>
      <span class="inc-when">${esc(fmtAge(i.created_at) || '')}</span>`;
    list.appendChild(row);
  }
}

// Re-render the visible Incidents tab from the store (e.g. when the env lens
// changes) without re-fetching.
export function renderIncidentsFromStore() { _renderFromStore(); }

export async function loadIncidents() {
  // Wire tab buttons if not already wired (they get recreated each nav-switch)
  const tabOpen = document.getElementById('inc-tab-open');
  const tabHist = document.getElementById('inc-tab-history');
  if (tabOpen && !tabOpen.dataset.wired) {
    tabOpen.dataset.wired = '1';
    tabOpen.addEventListener('click', () => {
      incidentsTab = 'open';
      tabOpen.classList.add('active'); tabHist.classList.remove('active');
      _renderFromStore();
    });
    tabHist.addEventListener('click', () => {
      incidentsTab = 'history';
      tabHist.classList.add('active'); tabOpen.classList.remove('active');
      _renderFromStore();
    });
  }

  const list  = document.getElementById('incidents-list');
  const empty = document.getElementById('incidents-empty');
  list.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';
  empty.style.display = 'none';

  // Subscribe to future store updates so the visible tab stays in sync when
  // the store is refreshed by SSE deltas or action callbacks elsewhere.
  // Replace any previous subscription to avoid stacking listeners.
  if (_unsubscribe) _unsubscribe();
  _unsubscribe = subscribe(_renderFromStore);

  // Populate the store, then render.
  await refresh();
  _renderFromStore();
}
