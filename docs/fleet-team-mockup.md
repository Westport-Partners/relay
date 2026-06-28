# Fleet board — team level, single environment (ASCII canvas)

Real test data. **Team node** = receives one product line's data (here *Primary
Product Line*). **Single environment** selected = `prod`.

Org shape in this slice (line › product › component › project):

```
Primary Product Line
├─ Application Filing (product)
│   ├─ Intake Service (component)        → primary-intake-api-prod      degraded · 5 open · SEV3
│   └─ Validation Engine (component)     → primary-validation-svc-prod  degraded · 6 open · SEV3
└─ Examination (product)
    ├─ Examiner Workbench (component)    → primary-workbench-web-prod   degraded · 5 open · SEV3
    └─ Prior-Art Search (component)      → primary-search-svc-prod      degraded · 8 open · SEV3
```

---

## Layout sketch — nested boxes, children side-by-side

Environment is the outer container. Inside it: product line → products
side-by-side → components side-by-side → project tiles side-by-side.

```
┌─ ENV: PROD ─────────────────────────────────────────────────────────────────────────────┐
│                                                                                            │
│  ┌─ ● PRIMARY PRODUCT LINE ──────────────────────────────────────────  4D ─────────────┐  │
│  │                                                                                       │  │
│  │  ┌─ ● Application Filing ───────────────────┐  ┌─ ● Examination ─────────────────────┐ │  │
│  │  │                                          │  │                                     │ │  │
│  │  │  ┌─ ● Intake Service ──┐ ┌─ ● Validation │  │  ┌─ ● Examiner ──┐ ┌─ ● Prior-Art ─┐ │ │  │
│  │  │  │ primary-intake-api  │ │   Engine ─────│  │  │   Workbench ──│ │   Search ─────│ │ │  │
│  │  │  │ ▲ DEGRADED          │ │ primary-valid │  │  │ primary-work  │ │ primary-search│ │ │  │
│  │  │  │ ● 5 · SEV3   2s ago │ │ ▲ DEGRADED    │  │  │ ▲ DEGRADED    │ │ ▲ DEGRADED    │ │ │  │
│  │  │  └─────────────────────┘ │ ● 6·SEV3  2s  │  │  │ ● 5·SEV3  2s  │ │ ● 8·SEV3  2s  │ │ │  │
│  │  │                          └───────────────│  │  └───────────────┘ └───────────────┘ │ │  │
│  │  └──────────────────────────────────────────┘  └─────────────────────────────────────┘ │  │
│  │                                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                            │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

Legend: `●`=status LED, `▲`=degraded marker, `● N·SEV3`=open incidents + worst
severity, `Ns ago`=last heartbeat. Group count chip `4D` = 4 degraded.

---

## Scratch area — edit below to show what you want

(Copy the box characters above and rearrange. Box-drawing chars to reuse:
`┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼ ─ │ ●  ▲`)

```
(your version here)
```
