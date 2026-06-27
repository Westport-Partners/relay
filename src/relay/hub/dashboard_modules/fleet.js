// Fleet big-board view — diff-renders the tile grid from the shared `tiles` Map,
// applies the active filter, and opens the tile detail drawer on click/keyboard.
// Ported from dashboard_parts/21-view-fleet-big-board.js.part (#33).

import { buildTile } from './helpers.js';
import { STATUS_ORDER } from './constants.js';
import { tiles, activeFilter } from './state.js';
import { openTile } from './tile-drawer.js';

export function renderAll() {
  const grid = document.getElementById('grid');
  const emptyState = document.getElementById('empty-state');
  const summary = document.getElementById('fleet-summary');

  let visible = Array.from(tiles.values());
  if (activeFilter === 'incidents') {
    visible = visible.filter(t => t.open_incidents > 0 || t.status === 'red' || t.status === 'degraded');
  }

  // Sort: worst-first, then most-recently-updated first.
  visible.sort((a, b) => {
    const so = (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99);
    if (so !== 0) return so;
    const ta = a.last_heartbeat_at ? new Date(a.last_heartbeat_at).getTime() : 0;
    const tb = b.last_heartbeat_at ? new Date(b.last_heartbeat_at).getTime() : 0;
    return tb - ta;
  });

  // Counts for summary bar.
  const counts = { red: 0, degraded: 0, grey: 0, green: 0 };
  for (const t of tiles.values()) counts[t.status] = (counts[t.status] || 0) + 1;
  summary.textContent =
    `${tiles.size} apps — ` +
    `${counts.red || 0} red · ${counts.degraded || 0} degraded · ` +
    `${counts.grey || 0} unknown · ${counts.green || 0} green`;

  if (visible.length === 0) {
    grid.innerHTML = '';
    emptyState.classList.add('visible');
    return;
  }
  emptyState.classList.remove('visible');

  // Diff-update: reuse existing DOM nodes, add/remove as needed.
  const existing = new Map();
  grid.querySelectorAll('.tile').forEach(el => existing.set(el.dataset.key, el));

  const frag = document.createDocumentFragment();
  for (const t of visible) {
    const key = t.account_id + '/' + t.app_name;
    // Always rebuild tile (cheap; avoids stale text nodes).
    if (existing.has(key)) existing.get(key).remove();
    frag.appendChild(buildTile(t));
  }
  grid.innerHTML = '';
  grid.appendChild(frag);
}

// Refresh "last seen N ago" text every 10s without a server round-trip.
setInterval(() => { if (tiles.size > 0) renderAll(); }, 10_000);

// Delegated tile activation — open the detail drawer on click or keyboard.
// Attached once to the grid container so it survives diff-render rebuilds.
export function wireTileActivation() {
  const grid = document.getElementById('grid');
  if (!grid) return;
  const activate = el => {
    const tile = el.closest && el.closest('.tile');
    if (tile && tile.dataset.account) openTile(tile.dataset.account, tile.dataset.app);
  };
  grid.addEventListener('click', e => activate(e.target));
  grid.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(e.target); }
  });
}
