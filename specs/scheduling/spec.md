# Domain Spec: Scheduling

**Owns:** on-call schedule management — generating, storing, and querying a
role-aware coverage grid so the escalation path always resolves to a real person.

**Primary code:** `core/scheduling.py` (`auto_schedule`, `assignment_at`,
`assignments_at`, `apply_overrides`, `Role`, `ScheduledSlot`),
`core/role_resolver.py` (`ScheduleRoleResolver`, timezone resolution),
`adapters/aws/dynamo_stores.py` (`DynamoScheduleStore`),
`hub/app.py` (Schedule view + `PUT/DELETE /schedule/override`).
**status.md:** §3. **Related domains:** [contacts](../contacts/spec.md)
(the people placed into slots), [escalation](../escalation/spec.md) (calls
`role_resolver` to convert a role name → contact ids at page time),
[ui](../ui/spec.md) (schedule grid with gap highlighting).

## What it does now

- **Role-aware slots:** each `ScheduledSlot` carries a `Role` enum value
  (`primary`, `secondary`, `manager`). A slot always names exactly one person
  per role.
- **One-click auto-schedule** (`auto_schedule`): balances assignments across
  available contacts for a week; enforces `EXCLUSIVE_ROLES` so the same person
  cannot be both primary and secondary in a single slot; no double-booking.
- **Point-in-time lookup:** `assignment_at(when, role)` and `assignments_at(when)`
  return who is on call for a given moment — timezone-aware via
  `core/role_resolver.py`.
- **Ad-hoc overrides** (`apply_overrides`): cover-me / swap overrides stored in
  DynamoDB via `PUT/DELETE /schedule/override`; respected by all lookups.
- **Gap highlighting:** `hub/app.py` computes `coverage_by_role` and the Schedule
  view flags uncovered slots per role.
- `ScheduleRoleResolver` is wired into the Node's default construction
  (`node/handler.py:185-190`) so role→person resolution is live at page time.

## Key entities

- **`Role`** — enum: `primary`, `secondary`, `manager`.
- **`ScheduledSlot`** — `{ start, end, role, contact_id }`.
- **`ScheduleRoleResolver`** — resolves `Role` → `[contact_id]` at a given
  moment using the live DynamoDB schedule.
- **`DynamoScheduleStore`** — stores slots and overrides; queried per deployment.

## Invariants

- **AWS-free core:** `core/scheduling.py` and `core/role_resolver.py` contain
  no `boto3`; DynamoDB I/O is behind `DynamoScheduleStore`.
- **`EXCLUSIVE_ROLES` enforced:** auto-schedule never assigns the same contact as
  primary and secondary in the same slot.
- **Overrides take precedence:** `apply_overrides` is applied after base schedule
  generation; all downstream lookups see the overridden state.

## Out of scope (non-goals)

- **Round-robin rotation lists** — Relay generates a role-aware schedule from
  per-person availability instead of maintaining a hand-ordered rotation
  (status.md §3 ⛔).
- **Multi-week fairness / carryover** — `auto_schedule` is single-week greedy;
  no cross-week balance tracking (status.md §3 🗺️).
- **Manual override authoring UI** — the API (`PUT /schedule/override`) is done;
  the click-to-assign UI cell is roadmap (status.md §3 🗺️).
