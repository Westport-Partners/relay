---
description: Operate a live Relay incident — dashboard, acknowledge/resolve/ignore/route, AI briefing pack, and integration settings.
---

You are helping the user work a live incident using the Relay dashboard and HTTP API. The task covers reading the fleet big-board, opening the incident detail view, using the acknowledge/resolve/ignore/route actions, reading the AI briefing pack and AAR, managing routing and ignore rules, and wiring integration credentials on the Settings screen.

Read and follow **`prompts/operate-incident.md`** in this repo for the exact API endpoints, action semantics, and integration setup steps.

**Relay-specific reminders:**
- Write endpoints (acknowledge, resolve, ignore, route) require `RELAY_AUTH_MODE=alb` or `dev`. In `none` mode (the default) they return 403 — this is intentional for read-only internal networks.
- AI output is asynchronous and labeled AI-generated. The page fires immediately on `TRIGGERED` — never wait for the briefing before acknowledging.
- Ignoring an incident creates a persistent rule and auto-resolves it. Route creates a routing rule but does **not** resolve the current incident — future alarms only.
- The DB wins: routing and ignore rules are edited live in DynamoDB via the UI; the `routing.yaml` seed is not re-read on restart. Use "Download YAML" to re-sync Git with the live state.
- GitLab and ServiceNow integrations are pending validation in the current release; check `docs/integrations.md` for current status before configuring them.
