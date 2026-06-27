# Relay UI Design Language — "Industrial Command Center"

**This is the binding visual constitution for all Relay UI.** Every change to
`hub/dashboard.html` (or any future UI surface) must conform. It exists because
the UI was rebuilt once from this primer; keeping it canonical prevents drift
back to a generic SaaS look.

> Origin: promoted from `.debug/creative/ui-design-prompt.md` (the rebuild primer).
> That file is the seed; **this file is now canonical** — edit here, not there.

## Core philosophy

A mission-critical incident dashboard. The aesthetic is **Industrial Command
Center**: maximum data density, instant glanceability, zero visual friction.
It should read like specialized hardware, not a trendy web app — utilitarian,
serious, strictly functional.

## Forbidden (the "never" list)

- **No soft/rounded aesthetics.** Max border radius is `rounded-sm`.
- **No pastel or muted semantic colors.** Status colors are highly saturated, unmistakable.
- **No deep drop shadows.** Separate elements with 1px borders (`border-gray-700`/`-800`), not shadows.
- **No hidden information.** Don't bury actions or critical data behind hover/tooltips.
- **No centered fixed-width containers** (`max-w-7xl`). UI is full-bleed (`w-full`), uses the whole monitor.

## Typography

Legibility over elegance.

- **UI text:** utilitarian sans-serif (IBM Plex Sans / SF Pro / system sans).
- **Data & numbers:** **monospace** (IBM Plex Mono / JetBrains Mono) for *all* timestamps, IPs, incident IDs, phone numbers, metrics — so columns align.
- **Hierarchy:** small font sizes to maximize real estate; `uppercase tracking-wider text-xs` for table headers and section labels.

## Color palette

Dark-mode first (on-call engineers, middle of the night).

- **Background:** deep flat industrial grays/charcoal (`#111111`, `#1A1A1A`, `#0D0D0D`). **Never** blue-tinted SaaS darks like `slate-900`.
- **Text:** high-contrast off-white for primary data; medium gray for metadata.
- **Semantic (strict):**
  - **CRITICAL / down:** piercing red `#FF3333`
  - **WARNING / degraded:** hazard yellow-orange `#FFB000`
  - **HEALTHY / operational:** terminal green `#00FF00`
- **Indicator style:** glowing LED-style dots, thick colored left-borders on rows, or solid colored square badges.

## Westport brand reconciliation

The UI carries **two** palettes with non-overlapping jobs. Don't blend them.

- **Westport teal owns the chrome / identity** — source of truth is
  [`docs/stylesheets/brand.css`](../../docs/stylesheets/brand.css):
  - Primary `#005b6d`, light `#007489`, dark `#004858`, accent `#218993`.
  - Use for: the header/top bar, nav active markers, links, logo lockup, and any
    branded accent. This is what makes it unmistakably a Westport product.
- **The industrial palette owns operational data surfaces** — the Big Board
  tiles, status indicators, incident tables, the escalation timeline. Status
  semantics are **never** teal: down is `#FF3333`, degraded `#FFB000`, healthy
  `#00FF00`. Teal must never stand in for a status.

Rule of thumb: if it tells an on-call engineer *what is happening right now*, it
uses the industrial palette; if it's frame, identity, or navigation, it uses
Westport teal. When in doubt, status legibility wins over brand.

## Layout & spacing

- **Density:** minimal padding (`p-2`/`p-3`, never `p-6`/`p-8`).
- **Grid:** heavy visible 1px grid lines separate all panels (physical-dashboard feel).
- **Navigation:** persistent compact sidebar or minimal-height top bar.

## Per-surface constraints

- **Big Board:** dense grid/masonry of rigid identical cards — name, status indicator, uptime %. No illustrations.
- **Incidents list:** austere full-width data table; monospace `Time Open` / `Incident ID`; active rows tinted faint red/yellow.
- **Schedule / on-call:** horizontal Gantt-style timeline; a bright vertical red line marks "now."
- **Contacts:** rigid searchable directory; phone/email clickable with adjacent Copy icon.
- **Settings:** looks like a technical config panel; monospace input fields for URLs; Test Connection buttons that show raw HTTP response codes.

## How UI work is checked against this

Any UI PR is reviewed against this file as a checklist, and `/dod` requires the
change be exercised in a browser (not just unit-tested). A new UI surface that
violates the "never" list or the semantic palette is **NEEDS-ACTION**, not done.
