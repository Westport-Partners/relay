# tools/docs-shots — documentation screenshots & videos

Deterministic Playwright capture of Relay's UI for the docs site. No LLM in the
loop — every screen is a scripted navigation + selector wait + capture, so the
output is identical run to run against the seeded demo world.

See [`JOURNEYS.md`](./JOURNEYS.md) for the catalog of user journeys these
captures are built from (the source of truth; screenshots and videos both
derive from it).

## Prerequisites

1. The demo container running with the self-populating fake agency:

   ```bash
   RELAY_DEMO=true docker compose up -d --build   # from repo root
   ```

   This gives ~39 fleet tiles, 30 contacts, routing rules, and a drip of
   incidents, with `RELAY_AUTH_MODE=dev` so write actions don't 403.

2. Node 18+ and this folder's dev deps + the Chromium browser:

   ```bash
   cd tools/docs-shots
   npm install
   npx playwright install chromium
   ```

## Capture

```bash
# 1) Top up the demo world with deterministic capture state
#    (fires alarms, acks/resolves some, builds the schedule, forces a rules
#    deviation). Uses only the HTTP API — nothing a user couldn't do.
npm run seed

# 2) Screenshots -> docs/assets/screenshots/<page>/<name>.png
npm run shots

# 3) (optional) How-to videos -> tools/docs-shots/videos/*.webm
npm run video

# Subset while iterating:
node capture.mjs --only=B1,B3
```

`RELAY_BASE_URL` overrides the target (default `http://localhost:8080`).

## Output layout

Screenshots are grouped by the doc page they back, so authors can drop them in:

```
docs/assets/screenshots/
  operate/      S-FLEET-*, S-TILE-DRAWER, S-INCIDENT*, S-RULES, S-METRICS, S-MAINTENANCE
  scheduling/   S-CONTACTS, S-SCHEDULE, S-ONCALL
  configure/    S-RULES-DEVIATION
  integrations/ S-SETTINGS
```

Videos land in `tools/docs-shots/videos/` (git-ignored — publish to the docs
CDN / release assets, not the repo).

## Maintenance

Selectors are ground-truthed against `src/relay/hub/dashboard_modules/*` and
`dashboard_parts/02-body-shell.part.html`. If the dashboard markup changes and a
journey starts failing, the fix is in the corresponding journey's `run()` in
`capture.mjs` — that's the single maintenance point.

## Determinism notes

- The seed world is Faker-seeded, so the org tree, contacts, and tiles are
  stable. The only moving part is the incident drip (`RELAY_DEMO_MODE=drip`).
  For fully frozen captures run the container with `RELAY_DEMO_MODE=off` and let
  `npm run seed` create all incidents itself.
- Public-OSS safe: the demo agency is generic and unnamed; account IDs are
  obviously fake (`111111111111` / `222222222222`). Review any new screen for
  real identifiers before publishing.
