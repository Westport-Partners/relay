# Fleet board redesign — locked design decisions

> **Status: LOCKED direction (UI brainstorm, 2026-06-28).** This file is the
> human-readable source of truth for the agreed fleet-board design, so the intent
> survives even if the mockup code is broken or lost. Live mockup reference:
> `src/relay/hub/dashboard_modules/mock-fleet2.html` on branch
> `feat/fleet-env-nested-board`.

## The model (locked)

1. **Environment is the outer container.** One block per environment
   (dev/test/prod/pre-prod…), derived **dynamically** from the data — never a
   hardcoded list. Environments are never mixed within a block.
2. **AWS accounts NEVER appear** as an axis. Environment is the only zone axis.
   (Hard rule.)
3. **Inside each env block, the org tree nests**: product line › product ›
   component › project(=deployment tile). Depth is **dynamic** — render walks
   whatever `org_path` each tile has.
4. **Every level packs its children HORIZONTALLY** into a wrapping grid: products
   sit side by side; inside each product, components side by side; inside each
   component, project tiles side by side. Wrap to a new row only when the row is
   full. "Pack horizontal first, vertical only after."
   - **CRITICAL CSS:** `grid-template-columns: repeat(auto-fit, minmax(…,1fr))`.
     Use **auto-fit**, NOT auto-fill — auto-fill spawns phantom empty columns
     that squeeze nested boxes into one narrow track and force vertical stacking.
5. **One render path for Team and Central.** No separate team layout. A team
   Relay node simply receives less data from Event Hub (only its own account's
   deployments), so its tree nests shallower on its own.

## Header compression: COLLAPSE 1-CHILD (locked)

Decided after comparing four header treatments (Bars / Slim / Spine /
Collapse-1-child). **Decision: Collapse 1-child.**

**Rule:** when a structural node has exactly one child and no tiles of its own,
fold the child up into it, joining labels with ` › `. Repeat down the chain so a
run of single-child levels becomes ONE breadcrumb header instead of a stacked bar
per level.

**Why it wins for the team case:** a real team owns ONE component — same product
line, same product, same component — so line→product→component is a single-child
chain. Without collapse the team pays three nearly-empty header bars before
seeing a single tile. Collapse turns those three bars into one breadcrumb line.
For Central, multi-child levels stay expanded as normal nested boxes; collapse
only fires where a level is genuinely redundant.

---

## CENTRAL layout (multi-child levels stay expanded)

Real test data, env=`prod`, Primary Product Line slice:

```
┌─ ENV: PROD ─────────────────────────────────────────────────────────────────────────────┐
│  ┌─ ● PRIMARY PRODUCT LINE ──────────────────────────────────────────  4D ─────────────┐  │
│  │  ┌─ ● Application Filing ───────────────────┐  ┌─ ● Examination ─────────────────────┐ │  │
│  │  │  ┌─ ● Intake Service ──┐ ┌─ ● Validation┐│  │  ┌─ ● Examiner ──┐ ┌─ ● Prior-Art ─┐ │ │  │
│  │  │  │ primary-intake-api  │ │   Engine     ││  │  │   Workbench   │ │   Search      │ │ │  │
│  │  │  │ ▲ DEGRADED          │ │ primary-valid││  │  │ primary-work  │ │ primary-search│ │ │  │
│  │  │  │ ● 5 · SEV3   2s ago │ │ ▲ DEGRADED   ││  │  │ ▲ DEGRADED    │ │ ▲ DEGRADED    │ │ │  │
│  │  │  └─────────────────────┘ │ ● 6·SEV3  2s ││  │  └───────────────┘ └───────────────┘ │ │  │
│  │  │                          └──────────────┘│  └─────────────────────────────────────┘ │  │
│  │  └──────────────────────────────────────────┘                                          │  │
│  └───────────────────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

Primary Product Line has 2 products (Application Filing, Examination), each with
2 components — all multi-child, so all levels stay expanded.

---

## TEAM layout (single-child chain collapses to a breadcrumb)

Real test data — a team owns ONE component ("Classification"), 3 deployments
across dev/test/prod. Chain `Secondary Product Line › Registration ›
Classification` is single-child at every level → collapses to one header.

env=`prod` (single env selected):

```
┌─ ENV: PROD ───────────────────────────────────────────────────────────────────┐
│  ┌─ ● Secondary Product Line › Registration › Classification ───────  1D ────┐  │
│  │   ┌─ ● secondary-classify-svc-prod ─┐                                     │  │
│  │   │ ▲ DEGRADED                       │                                     │  │
│  │   │ ● 8 · SEV3        2s ago         │                                     │  │
│  │   └──────────────────────────────────┘                                     │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────┘
```

env=`ALL` (all environments, each its own outer container):

```
┌─ ENV: DEV ────────────────────────────────────────────┐
│  ┌─ ● Secondary…› Registration › Classification ─ 1D ┐ │
│  │   ┌─ ● secondary-classify-svc-dev ─┐               │ │
│  │   │ ▲ DEGRADED   ● 3·SEV4   2s ago │               │ │
│  │   └─────────────────────────────────┘               │ │
│  └──────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
┌─ ENV: TEST ───────────────────────────────────────────┐
│  ┌─ ● Secondary…› Registration › Classification ─ 1D ┐ │
│  │   ┌─ ● secondary-classify-svc-test ┐               │ │
│  │   │ ▲ DEGRADED   ● 4·SEV3   2s ago │               │ │
│  │   └─────────────────────────────────┘               │ │
│  └──────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
┌─ ENV: PROD ───────────────────────────────────────────┐
│  ┌─ ● Secondary…› Registration › Classification ─ 1D ┐ │
│  │   ┌─ ● secondary-classify-svc-prod ┐               │ │
│  │   │ ▲ DEGRADED   ● 8·SEV3   2s ago │               │ │
│  │   └─────────────────────────────────┘               │ │
│  └──────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

Legend: `●`=status LED, `▲`=degraded marker, `● N·SEVx`=open incidents + worst
severity, `Ns ago`=last heartbeat, count chip `1D`=1 degraded.

---

## Still open (future iteration)

- Rows vs Columns for the env containers (currently Rows; toggle exists in mockup).
- Tile content density (what each project tile shows).
- Whether to combine Slim header styling on top of Collapse (Collapse chosen as
  the structural rule; Slim styling could still thin the surviving headers).
- Dynamic env button group must derive from live envs (spec #40 hardcodes
  prod/test/dev; teams have dynamic envs incl. pre-prod).
