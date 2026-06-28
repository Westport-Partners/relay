# Domain Spec: Escalation

**Owns:** the escalation ladder — turning an unacknowledged incident into a
timed sequence of pages to primary → secondary → manager, stopping on ack.

**Primary code:** `core/escalation.py` (pure state machine), `node/handler.py`
(the paging path + timeout handling), `config/escalation.yaml` (policy seed).
**status.md:** §3. **Related domains:** [scheduling](../scheduling/spec.md)
(role→person), [engagement](../engagement/spec.md) (how a page is delivered),
[observability](../observability/spec.md) (timeline events).

## What it does now

- An **escalation policy** is an ordered list of `EscalationStep`s; each step
  names roles (primary/secondary/manager) and/or explicit contact_ids, the
  notify streams, and a timeout.
- `core/escalation.py` is a **pure state machine**: given the current step and
  an event (ack / timeout), it computes the next transition — `contact_ids_to_page`
  / `roles_to_page` for the step, or "exhausted." No I/O, no AWS, AWS-free.
- The Node paging path (`node/handler.py`) resolves roles→people (via
  [scheduling](../scheduling/spec.md)), dispatches pages through
  [engagement](../engagement/spec.md), and arms a one-shot timer for the step
  timeout (DynamoDB deadline swept by the always-on container).
- An **ack** stops the ladder. A **timeout** advances to the next step. Timeout
  handling is idempotent — re-firing the same timeout must not double-advance.
- On a real advance the container flips the incident to `ESCALATED` so the Hub
  sees a genuine state transition (dedup suppresses repeats).

## Key entities

- **EscalationPolicy** — ordered `EscalationStep`s, referenced by id from a
  routing rule.
- **EscalationStep** — `{ roles, contact_ids, streams, timeout }`. Validator
  requires ≥1 of roles/contacts.
- **escalation_policy_id** — which policy drove a given incident. Stamped on the
  `Incident` at classification (`node/handler.py`, from
  `Classification.escalation_policy_id`) so a historic incident's ladder stays
  reconstructable even if the policy is later edited. Legacy rows that predate the
  field read `None`; the flow view then falls back to the `policy_id` recorded in
  the immutable `incident.triggered` timeline event. Consumed by the process-flow
  view (`core/flow.py`, `GET /incidents/{id}/flow` — see
  [observability spec](../observability/spec.md)).

## Invariants

- **Pure core:** no `boto3` in `core/escalation.py`; timers/paging live in the Node.
- **Idempotent timeouts:** `on_timeout` re-fires must not append phantom state or pages.
- **Ack is terminal for the ladder:** once acknowledged, no further steps fire.

## Timeline events emitted (implemented in #19)

The Node paging path (`node/handler.py`) records four append-only
`TimelineEvent`s at the two escalation dispatch sites.  All use
`actor="system"`, `stream=Stream.TEAM`.

| `event_type` | Emitted when | Key `detail` fields |
|---|---|---|
| `incident.triggered` | `_handle_alarm` — escalation `start()` returns | `severity`, `signal_source`, `alarm_name`, `policy_id` |
| `escalation.page_sent` | After a page dispatches — step 0 in `_handle_alarm`; each advanced step in `_handle_timeout` | `step_index`, `roles`, `contact_ids` (resolved at page time), `streams`, `timeout_minutes` |
| `escalation.step_advanced` | `_handle_timeout` — real advance (`new_phase==ESCALATING`) | `from_step`, `to_step` |
| `escalation.exhausted` | `_handle_timeout` — `new_phase==EXHAUSTED` | `last_step_index` |

`contact_ids` in `escalation.page_sent` is the output of
`_contacts_for_transition(transition)` — the deduped role-resolved set captured
**at page time**, not "who's on call now" at read time.

### Idempotency guarantee

`on_timeout` re-fires return a no-op transition (`contact_ids_to_page == []`),
so the existing `if step_contacts:` block prevents phantom
`step_advanced`/`page_sent` events.  `escalation.exhausted` additionally guards
on the timeline (`any(ev.event_type == "escalation.exhausted" …)`) so a duplicate
EXHAUSTED transition appends nothing even when the incident is re-fetched.

These events unblock the timeline view in
[issue #20](https://github.com/Westport-Partners/relay/issues/20).

## Out of scope (non-goals)

- Round-robin rotation lists — Relay generates a role-aware schedule instead (status.md §3).
- Voice/phone escalation — email + SMS only.
