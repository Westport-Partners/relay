// Pure presentation helpers — escaping, time/age formatting, tile construction,
// metadata value rendering. Zero app-state dependencies. Everything imports from
// here; this imports only constants.
// Ported from dashboard_parts/20-shared-helpers.js.part plus the three formatters
// relocated here per the module map (metaValueHtml from 23, fmtTime/fmtDetail
// from 25) so the drawers no longer import a view (#33).

import { STATUS_LABEL, MARKER } from './constants.js';

export function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function fmtAge(isoStr) {
  if (!isoStr) return null;
  const secs = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return secs + 's ago';
  const m = Math.floor(secs / 60), s = secs % 60;
  if (m < 60) return m + 'm ' + s + 's ago';
  const h = Math.floor(m / 60), rm = m % 60;
  return h + 'h ' + rm + 'm ago';
}

export function ageClass(isoStr, liveness) {
  if (liveness === 'lost')  return 'lost';
  if (liveness === 'stale') return 'stale';
  return '';
}

export function abbrAccount(id) {
  // Show last 4 digits of account ID for density.
  return id && id.length > 4 ? '…' + id.slice(-4) : id;
}

export function fmtTime(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr);
  return isNaN(d) ? esc(isoStr) : d.toLocaleString();
}

// Timeline event detail is a dict {k: v}; render as " — k=v, k2=v2".
export function fmtDetail(detail) {
  if (!detail || typeof detail !== 'object') return detail ? ' — ' + esc(detail) : '';
  const parts = Object.entries(detail).map(([k, v]) => esc(k) + '=' + esc(typeof v === 'object' ? JSON.stringify(v) : v));
  return parts.length ? ' — ' + parts.join(', ') : '';
}

// pipeline_url → link, git_sha → abbreviated monospace; everything else escaped.
export function metaValueHtml(k, v) {
  if (k === 'pipeline_url' && typeof v === 'string' && /^https?:\/\//.test(v)) {
    return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>`;
  }
  if (k === 'git_sha' && typeof v === 'string' && v.length > 12) {
    return `<span style="font-family:monospace;" title="${esc(v)}">${esc(v.slice(0, 12))}</span>`;
  }
  return esc(String(v));
}

export function buildTile(t) {
  const status = t.status || 'grey';
  const div = document.createElement('div');
  div.className = 'tile ' + status;
  div.dataset.key = t.account_id + '/' + t.app_name;
  // The whole tile is an interactive control opening the detail drawer.
  div.dataset.account = t.account_id;
  div.dataset.app = t.app_name;
  div.setAttribute('role', 'button');
  div.setAttribute('tabindex', '0');
  div.setAttribute('aria-label', t.app_name + ' — ' + status + '. Open details.');

  const ageStr = fmtAge(t.last_heartbeat_at);
  const ageCls = ageClass(t.last_heartbeat_at, t.liveness);
  const label = (STATUS_LABEL[status] || (() => status))(t);
  const marker = MARKER[status] || '?';

  div.innerHTML = `
    <span class="tile-marker">${marker}</span>
    <div class="tile-app" title="${esc(t.app_name)}">${esc(t.app_name)}</div>
    <div class="tile-account">${esc(abbrAccount(t.account_id))}</div>
    <div class="tile-status-text">${esc(label)}</div>
    <div class="tile-meta">
      ${t.open_incidents > 0
        ? `<span class="tile-badge">&#9679; ${t.open_incidents} open</span>`
        : ''}
      ${ageStr
        ? `<span class="tile-last-seen ${ageCls}">${esc(ageStr)}</span>`
        : '<span class="tile-last-seen">never seen</span>'}
    </div>`;
  return div;
}
