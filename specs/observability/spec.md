# Domain Spec: Observability / Metrics

**Owns:** what an operator can *see and measure* about incidents — the incident
timeline, KPIs (MTTR, time-to-ack), the fleet big-board, and liveness.

**Primary code:** `core/metrics.py` (`compute_metrics`), `hub/health.py`
(`Liveness`, `FleetTile`), `core/model.py` (`TimelineEvent`), `hub/app.py`
(`GET /metrics`, `GET /incidents/{id}`). **status.md:** §8 (and the timeline
lives on the §5 incident model). **Related domains:**
[escalation](../escalation/spec.md) (emits timeline events),
[incident-records](../incident-records/spec.md) (owns the model),
[ui](../ui/spec.md) (renders all of this), [node-hub-federation](../node-hub-federation/spec.md)
(heartbeat feeds the board).

## What it does now

- **Timeline.** Each incident carries an **append-only, immutable** list of
  `TimelineEvent`s. Recorded `event_type`s:
  - `incident.triggered` — emitted when escalation starts (`_handle_alarm`);
    detail: `severity`, `signal_source`, `alarm_name`, `policy_id`.
  - `escalation.page_sent` — emitted once per actual page (step 0 on trigger;
    each advanced step on timeout); detail: `step_index`, `roles`,
    `contact_ids` (resolved at page time), `streams`, `timeout_minutes`.
  - `escalation.step_advanced` — emitted on a real timeout advance;
    detail: `from_step`, `to_step`.
  - `escalation.exhausted` — emitted when all escalation steps are exhausted;
    detail: `last_step_index`. Idempotent — at most one per incident.
  - `acknowledged`, `resolved`, `ignored`, `ai.brief`, `*.ticket_created`.
  All events ride in `TimelineEvent.detail`; the model carries no per-event fields.
- **Metrics.** `compute_metrics` reports MTTR, time-to-ack, incident counts, and
  `synthetic_total`; synthetic incidents are included on purpose (so a smoke
  test shows up end-to-end) and flagged in the Metrics view.
- **Fleet big-board.** A dense grid of every app's tile; fed by the container
  heartbeat. Net-new vs AWS Incident Manager.
- **Liveness.** `hub/health.py` classifies tiles LIVE / STALE / LOST from the
  per-minute heartbeat, so a silent app goes red.
- **Tile detail drawer.** Clicking a tile opens one data-driven drawer (on-call,
  hierarchy, metadata, AWS tags, open incidents) — sections render only when data exists.

## Key entities

- **TimelineEvent** — `{ event_type, at, detail }`, append-only.
- **Metrics rollup** — MTTR / time-to-ack / counts / `synthetic_total`.
- **FleetTile** — per-deployment board cell + `Liveness` state.

## Invariants

- **Timeline is append-only and immutable** — never edit or reorder past events.
- **Synthetic incidents count in metrics** deliberately (that's the verification);
  they're flagged, not hidden.

## In flight / planned

**[#20](https://github.com/Westport-Partners/relay/issues/20) — render expected vs. actual.**
On the incident card, show the **expected escalation ladder** (primary →
secondary → manager, each step's streams + timeout) as a spine, and slot the
**actual events** onto it with timestamps: page sent → no ack → escalated →
page sent → acknowledged by X → resolved by Y. Reached steps filled, unreached
ghosted; fall back to the current flat list when no flow data exists.
(orchestrator/Opus for spec+plan; see [ui spec](../ui/spec.md) for the visual.)

**Read path for #20:** derive the expected ladder from the incident's routing
rule → `escalation_policy_id` → policy steps. Likely add `escalation_policy_id`
to `Incident` so historic incidents stay reconstructable. Either a new
`GET /incidents/{id}/flow` (expected steps + actual timeline + contact-id→name map)
or an enriched incident detail. For a federated Hub with no `escalation.yaml`,
fall back to deriving the ladder from the recorded `escalation.page_sent` events.

Note: **[#19](https://github.com/Westport-Partners/relay/issues/19) is complete** —
the four escalation events described above are now emitted by `node/handler.py`
(`_handle_alarm` + `_handle_timeout`, via `_record_escalation_event`).
See [escalation spec](../escalation/spec.md) for the full event contract.

## Out of scope (non-goals)

- PDF export of the timeline/AAR (status.md §7 roadmap; markdown only today).
