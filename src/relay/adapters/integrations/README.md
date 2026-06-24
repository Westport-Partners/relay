# Relay adapters

An **adapter** connects Relay's incident lifecycle to an external system
(GitLab, ServiceNow, Microsoft Teams, …). Adapters are discovered automatically:
drop a package in this directory that exposes a `MANIFEST`, and the Hub wires it
in — **no edit to the Hub**. This is the contract for donating one.

## Layout (one folder per adapter)

```
src/relay/adapters/integrations/<name>/
  __init__.py     # public exports
  adapter.py      # MANIFEST + build(ctx)   ← the plug point (required)
  listener.py     # the IncidentListener (events → actions)
  sink.py         # the external client (HTTP, SDK, …), if any
  README.md       # what it does, config (env/settings), required scopes, events
```

`_template/` is a copy-paste skeleton. Copy it to `<name>/`, rename the classes,
and fill in the TODOs.

## The contract

### 1. `MANIFEST` (in `adapter.py`)

A module-level `relay.adapters.registry.AdapterManifest`:

```python
MANIFEST = AdapterManifest(
    name="myservice",                 # stable short id
    build=build,                      # build(ctx) -> IncidentListener | None
    events=(IncidentLifecycleEvent.TRIGGERED, IncidentLifecycleEvent.RESOLVED),
    required_env=("RELAY_MYSERVICE_TOKEN_SECRET",),  # docs/preflight only
    settings_keys=(),                                # runtime settings keys read
)
```

### 2. `build(ctx)` — the factory

```python
def build(ctx: AdapterContext) -> MyListener | None:
    sink = MySink.from_env(secret_fetcher=ctx.secret_fetcher)
    if sink is None:
        return None          # not configured → Hub runs without it (no error)
    return MyListener(sink, ctx.incident_store)
```

- **Return `None` when not configured.** Don't raise. A bare Hub simply has
  fewer listeners.
- Pull dependencies from `AdapterContext` (never construct AWS clients or read
  global state directly): `incident_store`, `settings_store`, `dashboard_url`,
  `secret_fetcher(name)->str`, `deployment_resolver(deployment_id, key)->str|None`,
  `attach_ai_brief`.
- **Own your config.** Read your own `RELAY_<NAME>_*` env vars inside a
  `from_env()` classmethod on your sink — don't expect the Hub to parse them.

### 3. The listener (`IncidentListener`)

```python
class MyListener:
    def on_event(self, *, event: IncidentLifecycleEvent, incident: Incident) -> None:
        if event == IncidentLifecycleEvent.TRIGGERED:
            ...   # e.g. open a ticket; stamp the id back on the incident
        elif event == IncidentLifecycleEvent.RESOLVED:
            ...   # e.g. close the ticket
```

- No-op for events you don't care about.
- The registry already isolates failures (one bad listener can't break the
  others or block incident flow), but keep `on_event` best-effort anyway.
- **Stash external ids on `incident.external_tickets`** via
  `incident.set_ticket("<system>_id", external_id)` on create, and read them
  back with `incident.get_ticket("<system>_id")` on resolve/close — the core
  model carries no per-integration field, so this generic map is the contract.
- To record a `<system>.ticket_created` timeline event (the durable audit
  record of the link), use `relay.adapters._support.record_sink_event(...)`.
- For notification deep-links, use
  `relay.adapters._support.incident_dashboard_links(...)`.

### 4. Configuration conventions

- **Secrets** come via `ctx.secret_fetcher` (Secrets Manager in prod) — never
  import `boto3` in an adapter module.
- **Runtime, UI-editable config** (e.g. a webhook URL or token a user pastes in
  the Settings screen) goes in the settings store under a key declared in
  `relay.core.settings.SettingsKey`.
- **Per-deployment routing** (which project/service an incident belongs to) uses
  `ctx.deployment_resolver(incident.deployment_id, "<your_key>")` against the
  catalog/org tree — don't reach into the org tree yourself.

## Lifecycle events

`relay.core.lifecycle.IncidentLifecycleEvent`: `TRIGGERED`, `ACKNOWLEDGED`,
`ESCALATED`, `RESOLVED`. The Hub emits TRIGGERED/ESCALATED off the bus and
ACKNOWLEDGED/RESOLVED from its API endpoints.

## Not integrations

The sibling `../aws/` (CloudWatch, SNS, DynamoDB, EventBridge — the AWS
substrate) and `../ai/` (AI providers, selected by their own factory) are **not**
lifecycle integrations. They live outside `integrations/`, so the registry —
which scans only `integrations/` — never sees them. See
[`../README.md`](../README.md) for the three adapter categories.

## Checklist for a donated adapter

- [ ] Folder `src/relay/adapters/integrations/<name>/` with `__init__.py`, `adapter.py`,
      `listener.py`, (`sink.py`), `README.md`.
- [ ] `adapter.py` exposes `MANIFEST` and `build(ctx) -> listener | None`.
- [ ] `build` returns `None` when unconfigured; no exceptions on the happy path.
- [ ] Config read via `from_env()` + `ctx.secret_fetcher`; settings keys in
      `SettingsKey`.
- [ ] Tests (unit-test the listener with a mock sink; the sink's `from_env`).
- [ ] README documents events handled, env/settings, and required scopes.
