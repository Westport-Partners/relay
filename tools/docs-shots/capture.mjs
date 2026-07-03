// Deterministic documentation capture for Relay.
//
// Drives the seeded demo container (http://localhost:8080) with headless
// Chromium and writes screenshots to docs/assets/screenshots/<page>/ and,
// with --video, per-journey webm videos to tools/docs-shots/videos/.
//
// No LLM in the loop: every step is an explicit navigation + selector wait +
// capture, so the output is identical run to run against the seeded world.
//
//   node capture.mjs            # screenshots only
//   node capture.mjs --video    # screenshots + paced how-to videos
//   node capture.mjs --only=B1,B3
//
// Selectors are ground-truthed against src/relay/hub/dashboard_modules/* and
// dashboard_parts/02-body-shell.part.html. If the UI markup changes, update the
// helpers here — that's the single maintenance point.

import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { mkdirSync, renameSync, existsSync } from 'node:fs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..', '..');
const SHOTS_DIR = resolve(REPO_ROOT, 'docs', 'assets', 'screenshots');
const VIDEO_DIR = resolve(HERE, 'videos');

const BASE = process.env.RELAY_BASE_URL || 'http://localhost:8080';
const VIEWPORT = { width: 1440, height: 900 };
const VIDEO = process.argv.includes('--video');
const ONLY = (process.argv.find((a) => a.startsWith('--only=')) || '').replace('--only=', '')
  .split(',').filter(Boolean);

// --- small helpers ---------------------------------------------------------

const log = (...a) => console.log('[capture]', ...a);

// Screenshots group under a doc-page subfolder so authors know where each goes.
async function shot(page, pageDir, name) {
  const dir = resolve(SHOTS_DIR, pageDir);
  mkdirSync(dir, { recursive: true });
  const file = resolve(dir, `${name}.png`);
  await page.screenshot({ path: file, animations: 'disabled' });
  log(`  shot ${pageDir}/${name}.png`);
}

// Click a top-nav view button and wait for its view container to be active.
async function gotoView(page, view) {
  await page.click(`.nav-btn[data-view="${view}"]`);
  await page.waitForSelector(`#view-${view}.active`, { timeout: 10_000 });
  await settle(page);
}

// Short paint settle. We do NOT wait for 'networkidle' — the dashboard holds a
// long-lived SSE connection (GET /stream), so networkidle never fires and would
// burn the full timeout on every call. Selector waits (waitForSelector) are the
// real synchronization; this is just a paint/animation cushion.
async function settle(page, ms = 400) {
  await page.waitForTimeout(ms);
}

// Pace a step when recording video so viewers can follow; near-instant for stills.
async function beat(page) {
  if (VIDEO) await page.waitForTimeout(1200);
}

// --- journeys --------------------------------------------------------------
// Each journey is { id, title, run(page) }. run() navigates + captures.

const JOURNEYS = [
  {
    id: 'B1', title: 'Fleet big-board', video: 'V-FLEET-TOUR',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      // Wait for tiles to render from the SSE/fleet load.
      await page.waitForSelector('#fleet-groups .tile', { timeout: 15_000 });
      await settle(page);
      await shot(page, 'operate', 'S-FLEET-ALL');
      await beat(page);

      // Environment lens -> prod (buttons injected by env-filter.js).
      const prod = page.locator('#env-filter .filter-btn[data-env="prod"]');
      if (await prod.count()) {
        await prod.first().click();
        await settle(page);
        await shot(page, 'operate', 'S-FLEET-PROD');
        await beat(page);
        // reset to ALL
        await page.locator('#env-filter .filter-btn[data-env="all"]').first().click().catch(() => {});
        await settle(page);
      } else {
        log('  (no prod env button — skipping S-FLEET-PROD)');
      }

      // "Incidents only" filter (fleet-only filter bar).
      const incOnly = page.locator('#filter-bar .filter-btn[data-filter="incidents"]');
      if (await incOnly.count()) {
        await incOnly.first().click();
        await settle(page);
        await shot(page, 'operate', 'S-FLEET-INCIDENTS-ONLY');
        await page.locator('#filter-bar .filter-btn[data-filter="all"]').first().click().catch(() => {});
        await settle(page);
      }
    },
  },
  {
    id: 'B2', title: 'Tile detail drawer', video: 'V-FLEET-TOUR',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('#fleet-groups .tile', { timeout: 15_000 });
      await settle(page);
      // Prefer a tile that is not healthy (has a status marker) for a rich drawer.
      const tile = page.locator('#fleet-groups .tile').first();
      await tile.click();
      // Drawer shares #drawer with incidents; wait for it to open.
      await page.waitForSelector('#drawer.open', { timeout: 10_000 });
      await settle(page);
      await shot(page, 'operate', 'S-TILE-DRAWER');
      await beat(page);
      await page.keyboard.press('Escape').catch(() => {});
    },
  },
  {
    id: 'B3', title: 'Respond to an incident', video: 'V-INCIDENT-RESPONSE',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'incidents');
      await page.waitForSelector('#incidents-list .inc-row', { timeout: 15_000 });
      await shot(page, 'operate', 'S-INCIDENTS-LIST');
      await beat(page);

      // Open the first incident's drawer.
      await page.locator('#incidents-list .inc-row').first().click();
      await page.waitForSelector('#drawer.open #btn-ack-inc', { timeout: 10_000 });
      await settle(page);
      await shot(page, 'operate', 'S-INCIDENT-DETAIL');
      await beat(page);

      // AI briefing pane (deterministic fallback when AI disabled).
      const briefBtn = page.locator('#btn-inc-brief');
      if (await briefBtn.count()) {
        await briefBtn.click();
        await page.waitForSelector('#inc-ai-panel', { state: 'visible', timeout: 10_000 }).catch(() => {});
        await settle(page, 800);
        await shot(page, 'operate', 'S-INCIDENT-BRIEF');
        await beat(page);
      }

      // Acknowledge (cancels escalation timer).
      const ack = page.locator('#btn-ack-inc');
      if (await ack.isEnabled().catch(() => false)) {
        await ack.click();
        await settle(page, 800);
        await shot(page, 'operate', 'S-INCIDENT-ACK');
        await beat(page);
      }
      await page.keyboard.press('Escape').catch(() => {});
    },
  },
  {
    id: 'B4B5', title: 'Ignore / route rule from incident', video: 'V-NOISE-CONTROL',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'incidents');
      await page.waitForSelector('#incidents-list .inc-row', { timeout: 15_000 });
      await page.locator('#incidents-list .inc-row').first().click();
      await page.waitForSelector('#drawer.open #btn-rule-inc', { timeout: 10_000 });
      await settle(page);

      // "Add rule…" opens one panel with an Ignore/Route toggle.
      await page.locator('#btn-rule-inc').click();
      await page.waitForSelector('#inc-rule-panel', { state: 'visible', timeout: 10_000 });
      await settle(page);
      // Default is ignore.
      await shot(page, 'operate', 'S-INCIDENT-IGNORE-FORM');
      await beat(page);

      // Flip to the Route form.
      const routeToggle = page.locator('#rule-action-route');
      if (await routeToggle.count()) {
        await routeToggle.click();
        await settle(page, 500);
        await shot(page, 'operate', 'S-INCIDENT-ROUTE-FORM');
        await beat(page);
      }
      await page.keyboard.press('Escape').catch(() => {});
    },
  },
  {
    id: 'B6', title: 'Manage routing & ignore rules', video: 'V-NOISE-CONTROL',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'rules');
      await settle(page, 800);
      await shot(page, 'operate', 'S-RULES');
      await beat(page);
      // Deviation banner (seed forces one). Capture whatever the rules view shows.
      await shot(page, 'configure', 'S-RULES-DEVIATION');
    },
  },
  {
    id: 'B7', title: 'Contacts & availability', video: null,
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'contacts');
      await settle(page, 800);
      await shot(page, 'scheduling', 'S-CONTACTS');
      await beat(page);
    },
  },
  {
    id: 'B8', title: 'On-call schedule', video: 'V-SCHEDULING',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'schedule');
      await settle(page, 1000);
      await shot(page, 'scheduling', 'S-SCHEDULE');
      await beat(page);
      await gotoView(page, 'oncall');
      await settle(page, 600);
      await shot(page, 'scheduling', 'S-ONCALL');
      await beat(page);
    },
  },
  {
    id: 'B9', title: 'Metrics', video: null,
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'metrics');
      await settle(page, 800);
      await shot(page, 'operate', 'S-METRICS');
      await beat(page);
    },
  },
  {
    id: 'B10', title: 'Settings / integrations', video: 'V-SETTINGS',
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'settings');
      await settle(page, 800);
      await shot(page, 'integrations', 'S-SETTINGS');
      await beat(page);
    },
  },
  {
    id: 'B11', title: 'Maintenance', video: null,
    async run(page) {
      await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      await gotoView(page, 'maintenance');
      await settle(page, 800);
      await shot(page, 'operate', 'S-MAINTENANCE');
      await beat(page);
    },
  },
];

// --- runner ----------------------------------------------------------------

async function main() {
  const selected = ONLY.length
    ? JOURNEYS.filter((j) => ONLY.includes(j.id))
    : JOURNEYS;
  log(`base=${BASE} video=${VIDEO} journeys=${selected.map((j) => j.id).join(',')}`);

  const browser = await chromium.launch();
  let failures = 0;

  if (VIDEO) {
    // One context (and one video file) per journey so each how-to is separate.
    for (const j of selected) {
      mkdirSync(VIDEO_DIR, { recursive: true });
      const ctx = await browser.newContext({
        viewport: VIEWPORT,
        recordVideo: { dir: VIDEO_DIR, size: VIEWPORT },
      });
      const page = await ctx.newPage();
      // Playwright writes videos with a GUID name and only exposes the path
      // after the context closes; capture the handle now so we can rename it.
      const video = page.video();
      try {
        log(`▶ ${j.id} ${j.title}`);
        await j.run(page);
      } catch (e) {
        failures++;
        log(`  FAIL ${j.id}: ${e.message}`);
      } finally {
        await page.close();
        await ctx.close(); // finalizes the video file
        // Rename the GUID file to the journey's video label (or its id).
        // Prefix with the journey id so journeys that share a video label
        // (e.g. B1+B2 both feed V-FLEET-TOUR) don't overwrite each other.
        const label = `${j.id}-${j.video || j.id}`;
        try {
          const src = video ? await video.path() : null;
          if (src && existsSync(src)) {
            const dest = resolve(VIDEO_DIR, `${label}.webm`);
            renameSync(src, dest);
            log(`  video ${label}.webm`);
          } else {
            log(`  video saved for ${j.id} (${label}) in ${VIDEO_DIR}`);
          }
        } catch (e) {
          log(`  video rename skipped for ${j.id}: ${e.message}`);
        }
      }
    }
  } else {
    const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    for (const j of selected) {
      try {
        log(`▶ ${j.id} ${j.title}`);
        await j.run(page);
      } catch (e) {
        failures++;
        log(`  FAIL ${j.id}: ${e.message}`);
      }
    }
    await ctx.close();
  }

  await browser.close();
  log(`done. ${failures} failing journey(s).`);
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error('[capture] fatal', e); process.exit(1); });
