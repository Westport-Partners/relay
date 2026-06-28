// Pure grouping logic for the Fleet big-board view — takes a flat array of
// tile objects and a (possibly null) rollup tree, returns a Group[] sorted
// worst-status-first, with any ungrouped tiles always last.
// No DOM, no fetch, no side-effects. Imports only from ./constants.js.

import { STATUS_ORDER } from './constants.js';

// Build a flat index of rollup nodes by id (walks the recursive children tree).
function indexRollup(nodes, out = new Map()) {
  if (!nodes) return out;
  for (const node of nodes) {
    out.set(node.id, node);
    if (node.children && node.children.length) {
      indexRollup(node.children, out);
    }
  }
  return out;
}

// Determine the worst tile status from a list of tiles.
function worstStatus(tiles) {
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
function tallyTiles(tiles) {
  const counts = { red: 0, degraded: 0, grey: 0, green: 0 };
  for (const t of tiles) {
    const s = t.status || 'grey';
    if (s in counts) counts[s]++;
  }
  return counts;
}

// Sort tiles within a group: worst-status-first, then last_heartbeat_at desc.
function sortTiles(tiles) {
  return [...tiles].sort((a, b) => {
    const so = (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99);
    if (so !== 0) return so;
    const ta = a.last_heartbeat_at ? new Date(a.last_heartbeat_at).getTime() : 0;
    const tb = b.last_heartbeat_at ? new Date(b.last_heartbeat_at).getTime() : 0;
    return tb - ta;
  });
}

/**
 * Group an array of tile objects by their product-line (root org_path node).
 *
 * @param {object[]} tilesArray  - Visible tile objects (already filtered).
 * @param {object[]|null} rollup - /fleet/rollup JSON (recursive tree), or null.
 * @returns {Group[]} Sorted worst-status-first; __ungrouped__ always last.
 *
 * Group shape: { key, label, level, status, counts:{red,degraded,grey,green}, tiles[] }
 */
export function groupTiles(tilesArray, rollup) {
  const rollupIndex = rollup ? indexRollup(rollup) : new Map();

  // Partition by grouping key = root org_path node id.
  const buckets = new Map(); // key -> { label, level, nodeId, tiles[] }

  for (const t of tilesArray) {
    const orgPath = t.org_path;
    if (!orgPath || !Array.isArray(orgPath) || orgPath.length === 0) {
      // No org_path — ungrouped bucket.
      if (!buckets.has('__ungrouped__')) {
        buckets.set('__ungrouped__', { label: 'Ungrouped', level: null, nodeId: null, tiles: [] });
      }
      buckets.get('__ungrouped__').tiles.push(t);
    } else {
      const root = orgPath[0];
      const key = root.id;
      if (!buckets.has(key)) {
        // label = node names from root down to grouping level joined by ' › '
        // For root-level grouping that is just org_path[0].name.
        const label = root.name || key;
        const level = root.level || 0;
        buckets.set(key, { label, level, nodeId: key, tiles: [] });
      }
      buckets.get(key).tiles.push(t);
    }
  }

  // Build Group[] with rollup data where available, else compute from tiles.
  const groups = [];
  for (const [key, bucket] of buckets) {
    if (key === '__ungrouped__') continue; // deferred to end

    const rollupNode = bucket.nodeId ? rollupIndex.get(bucket.nodeId) : null;
    let status, counts;
    if (rollupNode) {
      status = rollupNode.status || worstStatus(bucket.tiles);
      counts = {
        red:      rollupNode.red_count      ?? 0,
        degraded: rollupNode.degraded_count  ?? 0,
        grey:     rollupNode.grey_count      ?? 0,
        green:    rollupNode.green_count     ?? 0,
      };
    } else {
      status = worstStatus(bucket.tiles);
      counts = tallyTiles(bucket.tiles);
    }

    groups.push({
      key,
      label: bucket.label,
      level: bucket.level,
      status,
      counts,
      tiles: sortTiles(bucket.tiles),
    });
  }

  // Sort groups worst-status-first.
  groups.sort((a, b) => (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99));

  // Append __ungrouped__ last (if any).
  if (buckets.has('__ungrouped__')) {
    const ub = buckets.get('__ungrouped__');
    groups.push({
      key: '__ungrouped__',
      label: 'Ungrouped',
      level: null,
      status: worstStatus(ub.tiles),
      counts: tallyTiles(ub.tiles),
      tiles: sortTiles(ub.tiles),
    });
  }

  return groups;
}
