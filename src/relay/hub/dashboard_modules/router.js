// View-switch nav + hash deep-link router. Owns activeView (via state) and lazily
// invokes each view's loader on switch. Deep links like #/incident/<id> open the
// Incidents view + that incident's drawer (used by the Teams "Open in Relay" link).
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
  maintenance: 'Maintenance', rules: 'Ignore Rules',
};

// Only the top-level nav buttons carry data-view. Other .nav-btn elements
// (e.g. the Open/History tabs inside the Incidents view) reuse the class for
// styling and must NOT trigger a view switch — otherwise activeView becomes
// undefined and every view is hidden (blank screen).
export function wireNav() {
  document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-btn[data-view]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const view = btn.dataset.view;
      setActiveView(view);
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      document.getElementById('view-' + view).classList.add('active');
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
    });
  });
}

export function navTo(view) {
  const btn = document.querySelector('.nav-btn[data-view="' + view + '"]');
  if (btn) btn.click();
}

export function handleHash() {
  const m = (location.hash || '').match(/^#\/incident\/(.+)$/);
  if (!m) return;
  const id = decodeURIComponent(m[1]);
  navTo('incidents');
  openIncident(id);
}
