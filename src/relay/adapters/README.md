# Relay adapters — three categories

Adapters connect Relay's core to the outside world. They come in three distinct
kinds, separated by folder so it's obvious how each is wired:

| Folder | Category | What it is | How it's wired | Donatable? |
|---|---|---|---|---|
| [`integrations/`](integrations/) | **Lifecycle integrations** | React to incident events (GitLab, ServiceNow, Teams, …) | **Auto-discovered** — any package exposing a `MANIFEST` is found by `registry.py` | **Yes** — this is the plugin surface |
| `aws/` | **Platform substrate** | The AWS bindings Relay runs on (CloudWatch source, SNS, DynamoDB stores, EventBridge transport, scheduler) | Wired explicitly in the Hub/Node composition root | No — it's the cloud binding |
| `ai/` | **AI providers** | Pluggable `AIAssistant` backends (Bedrock, OpenAI-compatible, Claude Code) | Selected by `ai/factory.py` via `RELAY_AI_PROVIDER` | Provider-style, different contract (`AIAssistant`) |

**Rule of thumb:** if it should react to incident lifecycle events and be
pluggable by dropping in a folder, it goes in `integrations/`. The registry scans
**only** `integrations/` — nothing else is discovered, so there's no skip-list to
maintain.

## Shared pieces (top level)

- `base.py` — the adapter Protocols (`AlertSource`, `Notifier`, `IncidentSink`,
  `Transport`, `AIAssistant`, …).
- `registry.py` — discovery + `AdapterContext` + `build_listeners`.
- `_support.py` — helpers shared across listeners (`record_sink_event`,
  `incident_dashboard_links`, the builtin `AIBriefListener`).

## Adding an integration

See [`integrations/README.md`](integrations/README.md) for the contract and
[`integrations/_template/`](integrations/_template/) for a copy-paste skeleton.
