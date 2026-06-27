// On-Call view — purely schedule-backed. Shows who Relay would page right now
// for each role (primary/secondary/manager), resolved from the generated
// schedule in the team timezone. Rotations were removed; manage coverage on
// the Schedule screen.
// Ported from dashboard_parts/28-view-oncall.js.part (#33 pilot view).

import { esc } from './helpers.js';
import { TEAM_TZ } from './state.js';
import { navTo } from './router.js';

const ONCALL_ROLE_ORDER = ['primary', 'secondary', 'manager'];
const ONCALL_ROLE_LABELS = { primary: 'Primary', secondary: 'Secondary', manager: 'Manager' };

export async function loadOncall() {
  const view = document.getElementById('view-oncall');
  view.innerHTML = '<div style="color:var(--text-dim);padding:20px;">Loading…</div>';
  let data;
  try {
    const r1 = await fetch('/oncall');
    if (!r1.ok) throw new Error('fetch failed');
    data = await r1.json();
  } catch (_) {
    view.innerHTML = '<div style="color:var(--red);padding:20px;">Failed to load on-call data.</div>';
    return;
  }

  const noc = data.now_on_call;

  // No schedule covers this moment.
  if (!noc) {
    view.innerHTML = `
      <div class="view-toolbar"><h2>On-Call</h2></div>
      <div style="color:var(--text-dim);padding:40px 0;text-align:center;font-size:15px;">
        No on-call coverage scheduled for right now.<br>
        Generate a schedule on the <b>Schedule</b> screen to set who is on call.
      </div>`;
    return;
  }

  const shift = esc(noc.shift || '');
  const roles = noc.roles || {};
  // Order known roles first, then any extra roles the API returned.
  const roleKeys = ONCALL_ROLE_ORDER.filter(r => r in roles)
    .concat(Object.keys(roles).filter(r => !ONCALL_ROLE_ORDER.includes(r)));

  const cards = roleKeys.map(role => {
    const r = roles[role] || {};
    const label = ONCALL_ROLE_LABELS[role] || (role.charAt(0).toUpperCase() + role.slice(1));
    const who = (r.contact_id && !r.gap)
      ? `<div class="oncall-now-who"><span style="color:var(--green);font-size:11px;vertical-align:middle;">&#9632;</span> ${esc(r.name || r.contact_id)}</div>`
      : `<div class="oncall-now-who" style="color:var(--red);"><span style="font-size:11px;vertical-align:middle;">&#9632;</span> COVERAGE GAP</div>`;
    return `
      <div class="oncall-card${(r.gap || !r.contact_id) ? ' gap' : ''}">
        <div class="oncall-card-title">${esc(label)}</div>
        <div class="oncall-card-team">${shift} shift</div>
        ${who}
      </div>`;
  }).join('');

  view.innerHTML = `
    <div class="view-toolbar"><h2>On-Call</h2></div>
    <div class="oncall-sub">On call right now &middot; ${shift} shift &middot; times in ${esc(TEAM_TZ)}</div>
    <div class="oncall-grid">${cards}</div>
    <div class="oncall-foot">Coverage is managed on the <a href="#" id="oncall-to-schedule">Schedule</a> screen.</div>`;

  const link = document.getElementById('oncall-to-schedule');
  if (link) link.addEventListener('click', (e) => { e.preventDefault(); navTo('schedule'); });
}
