# Microsoft Teams adapter

Posts an incident card to a Teams **Incoming Webhook** when an incident triggers.
Tier 1 (webhook to a standing channel) — no Graph API, no per-incident chat (that
is a future capability).

## Lifecycle events handled

| Event | Action |
|---|---|
| `TRIGGERED` | Post a MessageCard (+ plain-text fallback for Workflows webhooks) with a deep link back to the incident. |

## Configuration (settings store, UI-set)

| Key | Purpose |
|---|---|
| `teams_webhook_url` (`SettingsKey.TEAMS_WEBHOOK_URL`) | Teams Incoming Webhook URL, set on the Settings screen. Read live per event. |

The adapter is enabled whenever a settings store is available; if no webhook URL is
set, it no-ops per event. The webhook URL is **not** an env var — it is runtime-set
via the dashboard so teams can change channels without a redeploy.

## Files
- `notifier.py` — `TeamsWebhookNotifier` (card builder + POST), `build_test_card`/`send_test`.
- `listener.py` — `TeamsListener` (event → notify; injectable notifier factory + link builder).
- `adapter.py` — `MANIFEST` + `build(ctx)`.
