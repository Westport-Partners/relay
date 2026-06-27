# Domain Spec: ChatOps

**Owns:** Microsoft Teams integration — posting incident cards to a standing
channel webhook and (roadmap) creating per-incident group chats.

**Primary code:** `adapters/integrations/teams/notifier.py` (`TeamsNotifier`,
`build_test_card`, `send_test`), `adapters/integrations/teams/listener.py`
(`TeamsListener`), `adapters/integrations/teams/adapter.py` (MANIFEST).
**status.md:** §6. **Related domains:**
[integrations-config](../integrations-config/spec.md) (lifecycle seam that
triggers Teams notifications; webhook URL stored in settings store),
[incident-records](../incident-records/spec.md) (incident data rendered in cards),
[ui](../ui/spec.md) (Settings card for webhook URL + Test button).

## What it does now

- **Standing-channel webhook:** when a lifecycle event fires (TRIGGERED /
  RESOLVED / etc.), `TeamsListener` posts an incident card to the configured
  Teams channel via Classic webhook or Power Automate. The webhook URL is set
  on the Settings screen, stored in DynamoDB, and read fresh per event.
- **Lifecycle seam integration:** `TeamsListener` subscribes to
  `IncidentLifecycleEvent` via the standard adapter protocol
  (`core/lifecycle.py`). Per-listener failure isolation means a Teams outage
  never breaks paging.
- **Test endpoint:** `build_test_card` / `send_test` back the Settings-screen
  Test button, which validates the URL end-to-end without waiting for a real
  incident.
- **Standard adapter packaging:** the `MANIFEST` in `adapter.py` enables
  auto-discovery; the Hub discovers it with no Hub edit required.

## Key entities

- **`TeamsNotifier`** — HTTP client for the Teams webhook; builds adaptive cards.
- **`TeamsListener`** — `IncidentListener` implementation; decides what each
  lifecycle event means (TRIGGERED → post card, RESOLVED → update/post).
- **`MANIFEST`** (`AdapterManifest`) — declares the adapter's id, listener
  factory, and required settings keys.

## Invariants

- **Webhook URL sourced from settings store at event time** — never cached
  between events; Settings-screen change takes effect on the next event.
- **Failure-isolated:** a `TeamsListener` exception is logged and skipped;
  other listeners (GitLab, ServiceNow, AI brief) continue unaffected.

## Out of scope (non-goals)

- **Per-incident Teams group chat** (Graph API, auto-add on-call, seed context)
  — designed but not built (status.md §6 🗺️).
- **Chat commands** (run CLI from chat) — not targeted (status.md §6 ⛔).
