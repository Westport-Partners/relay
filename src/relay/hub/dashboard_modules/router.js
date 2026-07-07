// View-switch nav + hash deep-link router. Owns activeView (via state) and lazily
// invokes each view's loader on switch. Deep links like #/incident/<id> open the
// Incidents view + that incident's drawer (used by the Teams "Open in Relay" link).
// Each top-level view also has a stable hash route (e.g. #/contacts) so pages can
// be bookmarked and a refresh restores the current view (#79).
// Ported from dashboard_parts/22-shell-view-switch-nav-router....js.part (#33).

import { setActiveView } from './state.js';
import { loadIncidents } from './incidents.js';
import { openIncident } from './incident-drawer.js';
import { loadContacts } from './contacts.js';
import { loadMetrics } from './metrics.js';
import { loadOncall } from './oncall.js';
import { loadSettings } from './settings.js';
import { loadSchedule } from './schedule.js';
import { loadMaintenance } from './maintenance.js';
import { loadRules } from './rules.js';

const TITLES = {
  fleet: 'Fleet Status', incidents: 'Incidents', metrics: 'Metrics',
  contacts: 'Contacts', oncall: 'On-Call', settings: 'Settings', schedule: 'Schedule',
  maintenance: 'Maintenance', rules: 'Rules',
};

// Only the top-level nav buttons carry data-view. Other .nav-btn elements
// (e.g. the Open/History tabs inside the Incidents view) reuse the class for
// styling and must NOT trigger a view switch — otherwise activeView becomes
// undefined and every view is hidden (blank screen).

// Activate a view directly (without going through a button click). Updates the
// DOM, state, title strip, filter-bar visibility, and loads the view's data.
function activateView(view) {
  document.querySelectorAll('.nav-btn[data-view]').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector('.nav-btn[data-view="' + view + '"]');
  if (btn) btn.classList.add('active');
  setActiveView(view);
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  const viewEl = document.getElementById('view-' + view);
  if (viewEl) viewEl.classList.add('active');
  // Top strip: reflect the active view title (uppercased via CSS).
  const titleEl = document.getElementById('view-title');
  if (titleEl) titleEl.textContent = TITLES[view] || view;
  // Filter-bar only visible on Fleet view.
  document.getElementById('filter-bar').style.visibility = view === 'fleet' ? 'visible' : 'hidden';
  if (view === 'incidents') loadIncidents();
  if (view === 'metrics') loadMetrics();
  if (view === 'contacts') loadContacts();
  if (view === 'oncall') loadOncall();
  if (view === 'settings') loadSettings();
  if (view === 'schedule') loadSchedule();
  if (view === 'maintenance') loadMaintenance();
  if (view === 'rules') loadRules();
}

export function wireNav() {
  document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      // Push the view into the URL so it can be bookmarked/refreshed.
      history.pushState(null, '', '#/' + view);
      activateView(view);
    });
  });
}

export function navTo(view) {
  history.pushState(null, '', '#/' + view);
  activateView(view);
}

export function handleHash() {
  const hash = location.hash || '';

  // #/incident/<id> — open Incidents view and the specific incident drawer.
  const incidentMatch = hash.match(/^#\/incident\/(.+)$/);
  if (incidentMatch) {
    const id = decodeURIComponent(incidentMatch[1]);
    activateView('incidents');
    openIncident(id);
    return;
  }

  // #/<view> — restore a top-level view (e.g. after refresh or bookmark).
  const viewMatch = hash.match(/^#\/([^/]+)$/);
  if (viewMatch) {
    const view = viewMatch[1];
    if (TITLES[view]) {
      activateView(view);
      return;
    }
  }

  // No recognised hash — default to fleet.
  activateView('fleet');
}
