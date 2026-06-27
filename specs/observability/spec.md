# Domain Spec: Observability / Metrics

**Owns:** what an operator can *see and measure* about incidents — the incident
timeline, KPIs (MTTR, time-to-ack), the fleet big-board, and liveness.

**Primary code:** `core/metrics.py` (`compute_metrics`), `core/flow.py`
(`build_flow`), `hub/health.py` (`Liveness`, `FleetTile`), `core/model.py`
(`TimelineEvent`), `hub/app.py` (`GET /metrics`, `GET /incidents/{id}`,
`GET /incidents/{id}/flow`). **status.md:** §8 (and the timeline
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
- **Process-flow view.** The incident drawer renders the **expected escalation
  ladder** (primary → secondary → manager, each rung's notify-streams + timeout)
  as a spine, with the **actual events** slotted onto it: reached rungs filled
  with their page timestamp, unreached rungs ghosted, a red "now-line" before the
  first unreached rung. `core/flow.py` `build_flow(incident, policy, contacts)` is
  a pure, AWS-free transform that merges the policy with the timeline; the Hub
  serves it at `GET /incidents/{id}/flow`. `source` is `config` (full policy
  loaded, ghosted rungs shown), `derived` (federated Hub with no
  `escalation.yaml` — the ladder is reconstructed from the recorded
  `escalation.page_sent` events, labeled as such, no ghost rungs knowable), or
  `none` (no policy and no page events → the drawer falls back to the flat
  timeline list). `policy_id` is read from `Incident.escalation_policy_id`,
  falling back to the `incident.triggered` event's `policy_id` for legacy rows.

## Key entities

- **TimelineEvent** — `{ event_type, at, detail }`, append-only.
- **Flow view** — `{ expected_steps, actual_events, contacts, policy_id, source,
  fallback }` from `build_flow`; the merged expected-ladder-vs-actual structure.
- **Metrics rollup** — MTTR / time-to-ack / counts / `synthetic_total`.
- **FleetTile** — per-deployment board cell + `Liveness` state.

## Invariants

- **Timeline is append-only and immutable** — never edit or reorder past events.
- **Synthetic incidents count in metrics** deliberately (that's the verification);
  they're flagged, not hidden.

## Out of scope (non-goals)

- PDF export of the timeline/AAR (status.md §7 roadmap; markdown only today).
