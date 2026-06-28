// Fleet big-board view — renders env-container tiles from the shared `tiles`
// Map, applies the active env/filter lens, and opens the tile detail drawer on
// click/keyboard. Group status/counts are computed client-side from the visible
// in-env tiles (per-env scoping rules out a whole-fleet rollup; see FR-013).
// Ported from dashboard_parts/21-view-fleet-big-board.js.part (#33).
// Env-container rewrite: environment is the outer container; the org hierarchy
// nests dynamically inside each env block (#43).

import { buildTile } from './helpers.js';
import { STATUS_ORDER } from './constants.js';
import { tiles, activeFilter } from './state.js';
import { openTile } from './tile-drawer.js';
import { buildOrg, envsPresent, worstStatus, tallyTiles } from './fleet-groups.js';
import { matchesEnv } from './env-filter.js';

// Space-adaptive tile density.
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

// Walk every .org-tiles grid inside #fleet-groups and apply a count-aware
// --tile-min so tiles fill the row without phantom empty columns.
// Structural .org-children grids are left to the CSS fixed min.
function sizeGroupGrids() {
  const container = document.getElementById('fleet-groups');
  if (!container || !container.children.length) return;
  const gap = 6;  // matches .org-tiles gap in CSS
  for (const grid of container.querySelectorAll('.org-tiles')) {
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

// "Collapse 1-child" chain: when a node has exactly one child and no direct
// tiles, fold that child into it, joining labels with ' › '. Repeats down the
// chain so a run of single-child levels collapses to one breadcrumb header.
// Always-on in production (no HDR toggle).
function collapseChain(node) {
  let n = node;
  let label = n.label;
  while (n.children && n.children.length === 1 && (!n.tiles || !n.tiles.length)) {
    n = n.children[0];
    label += ' › ' + n.label;
  }
  return { ...n, label, level: node.level };
}

// Worst-status sort key for tiles (lower = worse).
function tileOrd(t) { return STATUS_ORDER[t.status] ?? 99; }

// Recursive bounded org box. Collapses single-child chains, lays children out
// horizontally via .org-children grid, and attaches deployment tiles in
// .org-tiles at leaf level (also sorted worst-first). Status/counts come from
// buildOrg, computed over the visible in-env tiles — correct per FR-013 (no
// rollup over hidden environments).
function orgEl(node) {
  node = collapseChain(node);
  const isLeaf = !node.children || !node.children.length;

  const div = document.createElement('div');
  div.className = 'org lvl' + Math.min(node.level, 3) + (isLeaf ? ' leaf' : '');

  const c = node.counts;
  const head = document.createElement('div');
  head.className = 'org-head';

  const led = document.createElement('span');
  led.className = 'led ' + (node.status || 'grey');

  const olbl = document.createElement('span');
  olbl.className = 'olbl';
  olbl.textContent = node.label;

  const ocount = document.createElement('span');
  ocount.className = 'ocount';
  ocount.textContent =
    (c.red      ? c.red      + 'R ' : '') +
    (c.degraded ? c.degraded + 'D ' : '') +
    (c.grey     ? c.grey     + '? ' : '') +
    c.green + 'G';

  head.appendChild(led);
  head.appendChild(olbl);
  head.appendChild(ocount);
  div.appendChild(head);

  if (node.children && node.children.length) {
    const kids = document.createElement('div');
    kids.className = 'org-children';
    for (const ch of node.children) kids.appendChild(orgEl(ch));
    div.appendChild(kids);
  }

  if (node.tiles && node.tiles.length) {
    const tg = document.createElement('div');
    tg.className = 'org-tiles';
    const sorted = [...node.tiles].sort((a, b) => tileOrd(a) - tileOrd(b));
    for (const t of sorted) tg.appendChild(buildTile(t));
    div.appendChild(tg);
  }

  return div;
}

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

  // Apply incidents-only filter.
  let visible = inEnv;
  if (activeFilter === 'incidents') {
    visible = visible.filter(
      t => t.open_incidents > 0 || t.status === 'red' || t.status === 'degraded'
    );
  }

  if (visible.length === 0) {
    container.innerHTML = '';
    emptyState.classList.add('visible');
    return;
  }
  emptyState.classList.remove('visible');

  // Preserve scroll position across re-renders.
  const scrollTop = container.scrollTop;

  // Determine env list. matchesEnv already filtered inEnv → visible, so
  // envsPresent(visible) returns [selectedEnv] when a specific env is active,
  // or all envs when activeEnv==='all'. No circular import needed.
  const envList = envsPresent(visible);

  const frag = document.createDocumentFragment();

  for (const env of envList) {
    const envTiles = visible.filter(
      t => (t.environment || '').toLowerCase() === env.toLowerCase()
    );
    if (!envTiles.length) continue;

    const block = document.createElement('div');
    block.className =
      'env-block' + (/prod/i.test(env) && !/pre/i.test(env) ? ' env-prod' : '');

    // Env block header.
    const head = document.createElement('div');
    head.className = 'env-head';

    const headLed = document.createElement('span');
    headLed.className = 'led ' + worstStatus(envTiles);

    const ename = document.createElement('span');
    ename.className = 'ename';
    ename.textContent = env;  // CSS text-transform: uppercase handles display

    const ec = tallyTiles(envTiles);
    const ecount = document.createElement('span');
    ecount.className = 'ecount';
    ecount.textContent =
      `${envTiles.length} apps · ${ec.red}R · ${ec.degraded}D · ${ec.green}G`;

    head.appendChild(headLed);
    head.appendChild(ename);
    head.appendChild(ecount);
    block.appendChild(head);

    // Env body: org tree rendered recursively.
    const body = document.createElement('div');
    body.className = 'env-body';

    const tree = buildOrg(envTiles, 0);
    for (const n of tree.nodes) body.appendChild(orgEl(n));

    // Org-less direct tiles at this env level (no structural org_path depth).
    if (tree.direct.length) {
      const tg = document.createElement('div');
      tg.className = 'org-tiles';
      const sorted = [...tree.direct].sort((a, b) => tileOrd(a) - tileOrd(b));
      for (const t of sorted) tg.appendChild(buildTile(t));
      body.appendChild(tg);
    }

    block.appendChild(body);
    frag.appendChild(block);
  }

  container.innerHTML = '';
  container.appendChild(frag);
  container.scrollTop = scrollTop;

  // Apply per-grid tile sizing now that sections are in the DOM.
  sizeGroupGrids();
}

// Refresh "last seen N ago" text every 10s without a server round-trip.
setInterval(() => {
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
