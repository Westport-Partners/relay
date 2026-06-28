// Fleet big-board view — renders grouped tile sections from the shared `tiles`
// Map, applies the active filter, fetches /fleet/rollup for group status/counts,
// and opens the tile detail drawer on click/keyboard.
// Ported from dashboard_parts/21-view-fleet-big-board.js.part (#33).
// US1 grouping rewrite: flat #grid → #fleet-groups with section.fleet-group (#37).

import { buildTile } from './helpers.js';
import { STATUS_ORDER } from './constants.js';
import { tiles, activeFilter } from './state.js';
import { openTile } from './tile-drawer.js';
import { groupTiles } from './fleet-groups.js';
import { matchesEnv } from './env-filter.js';

// Cached rollup tree from /fleet/rollup — refreshed best-effort every 10s.
let cachedRollup = null;

// US3: space-adaptive tile density.
// auto-fit collapses empty tracks; per-group --tile-min drives how wide tiles
// grow so few tiles fill the row and many tiles pack densely — no phantom columns.
const MIN_TILE = 150;  // px — legibility floor
const MAX_TILE = 320;  // px — growth ceiling

// Pure helper: given container width W, tile count n, and gap size,
// return the clamped --tile-min value in px.
function computeTileMin(W, n, gap, minTile, maxTile) {
  const colsMax = Math.max(1, Math.floor((W + gap) / (minTile + gap)));
  const cols = Math.min(n, colsMax);
  const tileMin = Math.floor((W - (cols - 1) * gap) / cols);
  return Math.min(Math.max(tileMin, minTile), maxTile);
}

// Walk every .fleet-group-grid inside #fleet-groups and apply a count-aware
// --tile-min so tiles fill the row without phantom empty columns.
function sizeGroupGrids() {
  const container = document.getElementById('fleet-groups');
  if (!container || !container.children.length) return;
  const gap = 1;
  for (const grid of container.querySelectorAll('.fleet-group-grid')) {
    const W = grid.clientWidth;
    if (W === 0) continue;  // not laid out / hidden
    const n = grid.querySelectorAll('.tile').length;
    if (n === 0) continue;
    const tileMin = computeTileMin(W, n, gap, MIN_TILE, MAX_TILE);
    grid.style.setProperty('--tile-min', tileMin + 'px');
  }
}

// Resize listener — attached once at module level; debounced 100ms.
// Calls sizeGroupGrids only (not a full renderAll) to preserve all state.
let _resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(sizeGroupGrids, 100);
});

async function fetchRollup() {
  try {
    const res = await fetch('/fleet/rollup');
    if (res.ok) {
      cachedRollup = await res.json();
    }
  } catch (_) {
    // Leave cachedRollup as-is; graceful degradation to tile-computed status.
  }
}

// Fire once at module init — fire-and-forget (no await).
fetchRollup();

export function renderAll() {
  const container = document.getElementById('fleet-groups');
  const emptyState = document.getElementById('empty-state');
  const summary = document.getElementById('fleet-summary');

  // Environment lens first — everything below (summary, group rollups, the
  // incidents-only filter) operates on the in-env set so counts stay coherent.
  const inEnv = Array.from(tiles.values()).filter(t => matchesEnv(t));

  // Counts for summary bar — over the in-env set.
  const counts = { red: 0, degraded: 0, grey: 0, green: 0 };
  for (const t of inEnv) counts[t.status] = (counts[t.status] || 0) + 1;
  summary.textContent =
    `${inEnv.length} apps — ` +
    `${counts.red || 0} red · ${counts.degraded || 0} degraded · ` +
    `${counts.grey || 0} unknown · ${counts.green || 0} green`;

  // Apply filter.
  let visible = inEnv;
  if (activeFilter === 'incidents') {
    visible = visible.filter(t => t.open_incidents > 0 || t.status === 'red' || t.status === 'degraded');
  }

  if (visible.length === 0) {
    container.innerHTML = '';
    emptyState.classList.add('visible');
    return;
  }
  emptyState.classList.remove('visible');

  // Preserve scroll position across re-renders.
  const scrollTop = container.scrollTop;

  const groups = groupTiles(visible, cachedRollup);
  const frag = document.createDocumentFragment();

  for (const group of groups) {
    const section = document.createElement('section');
    section.className = 'fleet-group';

    // Header row.
    const header = document.createElement('div');
    header.className = 'fleet-group-header';

    const led = document.createElement('span');
    led.className = 'fleet-group-led ' + (group.status || 'grey');

    const labelEl = document.createElement('span');
    labelEl.className = 'fleet-group-label';
    labelEl.textContent = group.label;

    const countsEl = document.createElement('span');
    countsEl.className = 'fleet-group-counts';
    const c = group.counts;
    countsEl.textContent =
      `${c.red} red · ${c.degraded} degraded · ${c.grey} unknown · ${c.green} green`;

    header.appendChild(led);
    header.appendChild(labelEl);
    header.appendChild(countsEl);

    // Tile grid.
    const gridDiv = document.createElement('div');
    gridDiv.className = 'fleet-group-grid';
    for (const t of group.tiles) {
      gridDiv.appendChild(buildTile(t));
    }

    section.appendChild(header);
    section.appendChild(gridDiv);
    frag.appendChild(section);
  }

  container.innerHTML = '';
  container.appendChild(frag);
  container.scrollTop = scrollTop;

  // Apply per-group tile sizing now that sections are in the DOM.
  sizeGroupGrids();
}

// Refresh "last seen N ago" text every 10s without a server round-trip.
// Also re-fetch rollup best-effort each interval (fire-and-forget).
setInterval(() => {
  fetchRollup();
  if (tiles.size > 0) renderAll();
}, 10_000);

// Delegated tile activation — open the detail drawer on click or keyboard.
// Attached once to the container so it survives diff-render rebuilds.
export function wireTileActivation() {
  const container = document.getElementById('fleet-groups');
  if (!container) return;
  const activate = el => {
    const tile = el.closest && el.closest('.tile');
    if (tile && tile.dataset.account) openTile(tile.dataset.account, tile.dataset.app);
  };
  container.addEventListener('click', e => activate(e.target));
  container.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(e.target); }
  });
}
