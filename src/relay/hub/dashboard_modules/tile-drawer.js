// Fleet tile detail drawer — one data-driven panel for BOTH topologies: each
// section renders only when its data is present. A team Hub fills on_call live
// (it owns the schedule); a federated Hub shows the on_call snapshot the owning
// team pushed up its heartbeat. Identical markup either way — the UI never forks
// on topology.
// Ported from dashboard_parts/25-drawer-fleet-tile-detail.js.part (#33).

import { esc, fmtAge, fmtTime, fmtDetail, metaValueHtml } from './helpers.js';
import { STATUS_LABEL, MARKER } from './constants.js';
import { drawer, drawerOverlay, closeDrawer, openIncident } from './incident-drawer.js';

export async function openTile(accountId, appName) {
  drawer.innerHTML = '<span class="close">&times;</span><div style="color:var(--text-dim);">Loading…</div>';
  drawer.querySelector('.close').addEventListener('click', closeDrawer);
  drawer.classList.add('open');
  drawerOverlay.classList.add('open');
  let t;
  try {
    const r = await fetch('/fleet/' + encodeURIComponent(accountId) + '/' + encodeURIComponent(appName));
    if (!r.ok) throw new Error('not found');
    t = await r.json();
  } catch (e) {
    drawer.innerHTML = '<span class="close">&times;</span><p style="color:var(--red);">Deployment not found.</p>';
    drawer.querySelector('.close').addEventListener('click', closeDrawer);
    return;
  }
  renderTile(t);
  // Incidents for this deployment load async — the panel is useful immediately.
  loadTileIncidents(t);
}

export function renderTile(t) {
  const status = t.status || 'grey';
  const statusLabel = (STATUS_LABEL[status] || (() => status))(t);
  const liveness = t.liveness || 'unknown';
  const path = Array.isArray(t.service_path) && t.service_path.length
    ? t.service_path.join(' › ') : (t.deployment_id || '—');

  // --- Hierarchy section (from org_path; collapses if absent) ---
  let hierHtml = '';
  if (Array.isArray(t.org_path) && t.org_path.length) {
    const rows = t.org_path.map(n =>
      `<span class="k">${esc(n.level || 'node')}</span><span>${esc(n.name || n.id || '')}</span>`
    ).join('');
    hierHtml = `<div class="section-title">Hierarchy</div><div class="kv">${rows}</div>`;
  }

  // --- Metadata section (owner / gitlab / free-form + aws_tags) ---
  const meta = (t.metadata && typeof t.metadata === 'object') ? t.metadata : {};
  let metaHtml = '';
  const metaRows = [];
  for (const [k, v] of Object.entries(meta)) {
    if (k === 'aws_tags') continue;      // rendered separately as chips
    if (k === 'resource_tags') continue; // rendered separately as chips
    if (v == null || typeof v === 'object') continue;
    metaRows.push(`<span class="k">${esc(k)}</span><span>${metaValueHtml(k, v)}</span>`);
  }
  if (metaRows.length) {
    metaHtml += `<div class="section-title">Metadata</div><div class="kv">${metaRows.join('')}</div>`;
  }
  const awsTags = (meta.aws_tags && typeof meta.aws_tags === 'object') ? meta.aws_tags : null;
  if (awsTags && Object.keys(awsTags).length) {
    const chips = Object.entries(awsTags).map(([k, v]) =>
      `<span class="tag-chip"><span class="tag-k">${esc(k)}</span><span class="tag-v">${esc(v)}</span></span>`
    ).join('');
    metaHtml += `<div class="section-title">AWS tags</div><div class="tag-grid">${chips}</div>`;
  }
  const resourceTags = (meta.resource_tags && typeof meta.resource_tags === 'object') ? meta.resource_tags : null;
  if (resourceTags && Object.keys(resourceTags).length) {
    const chips = Object.entries(resourceTags).map(([k, v]) =>
      `<span class="tag-chip"><span class="tag-k">${esc(k)}</span><span class="tag-v">${esc(String(v))}</span></span>`
    ).join('');
    metaHtml += `<div class="section-title">Resource tags</div><div class="tag-grid">${chips}</div>`;
  }

  // --- On-call section (live on a team hub; snapshot on a federated hub) ---
  let oncallHtml = '';
  const oc = t.on_call;
  if (oc && oc.roles && Object.keys(oc.roles).length) {
    const rows = Object.entries(oc.roles).map(([role, who]) => {
      const name = who && who.name ? who.name : null;
      const cell = name
        ? esc(name)
        : '<span style="color:var(--amber);">— gap —</span>';
      return `<span class="k">${esc(role)}</span><span>${cell}</span>`;
    }).join('');
    const srcNote = oc.source === 'team_snapshot'
      ? ' <span class="oncall-src">team snapshot</span>'
      : ' <span class="oncall-src">live</span>';
    const shift = oc.shift ? ' · ' + esc(oc.shift) : '';
    oncallHtml = `<div class="section-title">On-call${shift}${srcNote}</div><div class="kv">${rows}</div>`;
  }

  drawer.innerHTML = `
    <span class="close">&times;</span>
    <div class="drawer-header-row">
      <div>
        <h2>${esc(t.app_name)} <span class="tile-marker ${esc(status)}" style="font-size:13px;">${MARKER[status] || '?'}</span></h2>
        <div class="sub">${esc(t.account_id)} · ${esc(t.deployment_id || '—')}</div>
      </div>
    </div>
    <div class="kv">
      <span class="k">Status</span><span class="oncall-status-${esc(status)}">${esc(statusLabel)}</span>
      <span class="k">Liveness</span><span>${esc(liveness)}</span>
      <span class="k">Environment</span><span>${esc(t.environment || '—')}</span>
      <span class="k">Service</span><span>${esc(path)}</span>
      <span class="k">Open incidents</span><span>${esc(String(t.open_incidents ?? 0))}${t.worst_severity ? ' · worst ' + esc(t.worst_severity) : ''}</span>
      <span class="k">Last heartbeat</span><span>${esc(fmtAge(t.last_heartbeat_at) || 'never')}</span>
      <span class="k">Registered</span><span>${esc(fmtTime(t.registered_at))}</span>
    </div>
    ${oncallHtml}
    ${hierHtml}
    ${metaHtml}
    <div class="section-title">Open incidents</div>
    <div id="tile-incidents"><div style="color:var(--text-dim);">Loading…</div></div>`;
  drawer.querySelector('.close').addEventListener('click', closeDrawer);
}

export async function loadTileIncidents(t) {
  const box = document.getElementById('tile-incidents');
  if (!box) return;
  let list = [];
  try {
    const r = await fetch('/incidents?account_id=' + encodeURIComponent(t.account_id));
    if (r.ok) list = await r.json();
  } catch (_) { /* leave empty */ }
  // Narrow to this deployment (the endpoint filters by account, not deployment).
  const mine = (Array.isArray(list) ? list : []).filter(i =>
    i.app_name === t.app_name &&
    (!t.deployment_id || t.deployment_id === 'unknown' || i.deployment_id === t.deployment_id)
  );
  if (!mine.length) {
    box.innerHTML = '<div style="color:var(--text-dim);">No open incidents.</div>';
    return;
  }
  box.innerHTML = mine.map(i => `
    <div class="inc-row tile-inc-row" data-cid="${esc(i.correlation_id)}">
      <span class="inc-sev ${esc(i.severity || '')}">${esc(i.severity || '')}</span>${i.synthetic ? ' <span class="badge-synthetic">TEST</span>' : ''}
      <span class="inc-app">${esc(i.alarm_name || i.app_name || '')}</span>
      <span class="inc-when">${esc(fmtAge(i.created_at) || '')}</span>
    </div>`).join('');
  // Compose with the existing incident drawer.
  box.querySelectorAll('.tile-inc-row').forEach(row =>
    row.addEventListener('click', () => openIncident(row.dataset.cid)));
}
