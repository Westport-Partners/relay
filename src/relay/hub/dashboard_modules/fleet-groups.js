// Pure grouping logic for the Fleet big-board env-container board.
//
// API summary:
//   buildOrg(tiles, depth) → { nodes: OrgNode[], direct: Tile[] }
//     Recursively groups tiles by org_path structural levels (all entries
//     except the final deployment leaf). Tiles whose structural depth is
//     exhausted at `depth` land in `direct`; others are bucketed by
//     structural[depth].id (fallback .name) and recursed to depth+1.
//     OrgNode = { label, level, status, counts:{red,degraded,grey,green},
//                 tiles: Tile[], children: OrgNode[] }
//     Nodes are sorted worst-status-first.
//
//   envsPresent(tiles) → string[]
//     Deduped tile.environment values sorted by canonical promotion order,
//     then alpha for unknowns.
//
//   worstStatus(tiles) → string
//   tallyTiles(tiles) → { red, degraded, grey, green }
//     Internal helpers, also exported for use by the render layer.
//
// No DOM, no fetch, no side-effects. Imports only from ./constants.js.

import { STATUS_ORDER } from './constants.js';

// ── Status helpers ───────────────────────────────────────────────────────────

// Determine the worst tile status from a list of tiles.
export function worstStatus(tiles) {
  let best = 4; // beyond green
  for (const t of tiles) {
    const ord = STATUS_ORDER[t.status] ?? 99;
    if (ord < best) best = ord;
  }
  const entries = Object.entries(STATUS_ORDER);
  const found = entries.find(([, v]) => v === best);
  return found ? found[0] : 'grey';
}

// Tally {red,degraded,grey,green} counts from a list of tiles.
export function tallyTiles(tiles) {
  const counts = { red: 0, degraded: 0, grey: 0, green: 0 };
  for (const t of tiles) {
    const s = t.status || 'grey';
    if (s in counts) counts[s]++;
  }
  return counts;
}

// ── Org tree builder ─────────────────────────────────────────────────────────

/**
 * Recursively group tiles into a dynamic-depth org tree.
 *
 * org_path is [ {id, name, level}, … ] where the LAST entry is the deployment
 * leaf (the tile itself). Structural levels = org_path.slice(0, -1).
 *
 * At each call:
 *   - Tiles whose structural depth is exhausted (depth >= structural.length)
 *     attach as `direct` at this level.
 *   - Others are bucketed by structural[depth].id (fallback .name) and
 *     recursed to depth+1.
 *
 * Returned nodes are sorted worst-status-first via STATUS_ORDER.
 *
 * @param {object[]} tiles
 * @param {number}   depth  Starting depth (call with 0 from outside).
 * @returns {{ nodes: OrgNode[], direct: Tile[] }}
 */
export function buildOrg(tiles, depth) {
  const buckets = new Map(); // key → { node, tiles[] }
  const direct = [];

  for (const t of tiles) {
    const p = t.org_path || [];
    // structural levels = everything except the final deployment leaf
    const struct = p.slice(0, Math.max(0, p.length - 1));
    if (depth >= struct.length) {
      direct.push(t);
      continue;
    }
    const node = struct[depth];
    const k = node.id || node.name;
    if (!buckets.has(k)) buckets.set(k, { node, tiles: [] });
    buckets.get(k).tiles.push(t);
  }

  const nodes = [];
  for (const { node, tiles: bt } of buckets.values()) {
    const deeper = buildOrg(bt, depth + 1);
    nodes.push({
      label:    node.name || node.id,
      level:    depth,
      status:   worstStatus(bt),
      counts:   tallyTiles(bt),
      tiles:    deeper.direct,
      children: deeper.nodes,
    });
  }

  // Worst-status-first: lower STATUS_ORDER value = worse.
  nodes.sort((a, b) => (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99));

  return { nodes, direct };
}

// ── Environment enumeration ───────────────────────────────────────────────────

// Canonical promotion order for environment sorting.
const ENV_ORDER = ['dev', 'test', 'int', 'qa', 'stage', 'staging', 'pre-prod', 'preprod', 'prod', 'production'];

/**
 * Return the deduplicated set of environment names present in `tiles`,
 * sorted by canonical promotion order (case-insensitive), then alpha for
 * unknowns.
 *
 * @param {object[]} tiles
 * @returns {string[]}
 */
export function envsPresent(tiles) {
  const s = new Set(tiles.map(t => t.environment).filter(Boolean));
  return [...s].sort((a, b) => {
    const ia = ENV_ORDER.indexOf(a.toLowerCase());
    const ib = ENV_ORDER.indexOf(b.toLowerCase());
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
}
