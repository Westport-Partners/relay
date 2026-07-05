# Relay — Author an Integration Adapter Prompt

You are helping the user add a new external integration (ticketing system, chat tool, etc.) to Relay following the auto-discovery adapter convention. You will work from the template in `src/relay/adapters/integrations/_template/`. No changes to core are needed — drop a folder, implement the interface, and the registry discovers it at startup.

Canonical reference: [`docs/integrations.md`](../docs/integrations.md), [`src/relay/adapters/integrations/_template/README.md`](../src/relay/adapters/integrations/_template/README.md).

---

## Guardrails

- **Failure isolation is mandatory.** A broken or slow adapter must never delay paging or block other listeners. Catch all exceptions in `create_record` and `close_record`; return `""` (or `None`) on failure rather than raising.
- **Adapters are opt-in.** `build()` returns `None` when the integration is not configured (env var absent or secret fetch fails). A missing token never blocks startup.
- **No PII in Git.** The adapter reads credentials from environment variables / Secrets Manager via `secret_fetcher`. Never hardcode tokens, URLs, or usernames.
- **Core stays AWS-free.** Keep all cloud SDK calls behind the sink. Never import `boto3` into `src/relay/core/`.

---

## Step 1 — Copy the template

```bash
# Replace <name> with your integration name (lowercase, no underscore prefix)
cp -r src/relay/adapters/integrations/_template \
       src/relay/adapters/integrations/<name>
```

The `_template` prefix causes the registry to skip the original. Your new folder (without the underscore) is discovered automatically.

Files in the new folder:

| File | Purpose |
|------|---------|
| `adapter.py` | `MANIFEST` + `build(ctx)` factory — the plug point the registry reads |
| `listener.py` | Maps lifecycle events to sink actions |
| `sink.py` | External client; `from_env` factory; `create_record` / `close_record` |
| `README.md` | Document what the adapter does, which events it handles, and what env vars it reads |

---

## Step 2 — Implement `sink.py`

The sink is the only file that talks to the external service. It must:

1. Define a `Config` dataclass (rename from `TemplateConfig`) with the connection parameters.
2. Implement `from_env(secret_fetcher)` — read `RELAY_<NAME>_*` env vars, resolve secrets via `secret_fetcher`, return `None` to disable:

```python
@classmethod
def from_env(cls, secret_fetcher=None):
    secret_name = os.environ.get("RELAY_<NAME>_TOKEN_SECRET", "").strip()
    if not secret_name or secret_fetcher is None:
        return None
    try:
        token = secret_fetcher(secret_name) or ""
    except Exception:
        logger.warning("<name> secret fetch failed; adapter disabled")
        return None
    if not token:
        return None
    base_url = os.environ.get("RELAY_<NAME>_BASE_URL", "https://example.com")
    return cls(<Name>Config(token=token, base_url=base_url))
```

3. Implement `create_record(incident) -> str` — create the external record, return its ID (or `""` on failure):

```python
def create_record(self, incident):
    try:
        # Make your API call here
        # return the external record ID as a string
        return external_id
    except Exception:
        logger.warning("<name> create_record failed", exc_info=True)
        return ""
```

4. Implement `close_record(external_id, incident) -> None` — close the external record on resolve:

```python
def close_record(self, external_id, incident):
    try:
        # Make your API call here
        pass
    except Exception:
        logger.warning("<name> close_record failed", exc_info=True)
```

---

## Step 3 — Implement `listener.py`

The listener maps lifecycle events to sink actions. It receives every `IncidentLifecycleEvent` the adapter subscribes to. Use `record_sink_event` to stamp a timeline event on the incident:

```python
from relay.adapters._support import record_sink_event
from relay.core.lifecycle import IncidentLifecycleEvent

_SYSTEM = "<name>"

class <Name>Listener:
    def __init__(self, sink, incident_store):
        self._sink = sink
        self._incident_store = incident_store

    def on_event(self, *, event, incident):
        if event == IncidentLifecycleEvent.TRIGGERED:
            external_id = self._sink.create_record(incident)
            if external_id:
                incident.set_ticket(f"{_SYSTEM}_id", external_id)
                record_sink_event(incident, self._incident_store, _SYSTEM, external_id)

        elif event == IncidentLifecycleEvent.RESOLVED:
            external_id = incident.get_ticket(f"{_SYSTEM}_id")
            if external_id:
                self._sink.close_record(external_id, incident)
```

You may also handle `ACKNOWLEDGED` and `ESCALATED` if the integration supports it — add them to the `events` tuple in the manifest below.

---

## Step 4 — Implement `adapter.py`

The manifest is what the registry reads. Keep `build()` minimal:

```python
from relay.adapters.integrations.<name>.listener import <Name>Listener
from relay.adapters.integrations.<name>.sink import <Name>Sink
from relay.adapters.registry import AdapterContext, AdapterManifest
from relay.core.lifecycle import IncidentLifecycleEvent


def build(ctx: AdapterContext) -> <Name>Listener | None:
    sink = <Name>Sink.from_env(secret_fetcher=ctx.secret_fetcher)
    if sink is None:
        return None
    return <Name>Listener(sink, ctx.incident_store)


MANIFEST = AdapterManifest(
    name="<name>",
    build=build,
    events=(IncidentLifecycleEvent.TRIGGERED, IncidentLifecycleEvent.RESOLVED),
    required_env=("RELAY_<NAME>_TOKEN_SECRET",),
)
```

---

## Step 5 — Register in the manifest

Add the adapter to the registry manifest so it is included in the auto-discovery list. Check whether a `src/relay/adapters/integrations/MANIFEST` or similar registry file exists and follow its pattern to register your adapter.

---

## Step 6 — Test

Write a unit test in `tests/` that:

1. Patches the HTTP layer (do not make real API calls in tests).
2. Calls `on_event` with `TRIGGERED` and asserts `create_record` was called.
3. Calls `on_event` with `RESOLVED` and asserts `close_record` was called.
4. Calls `from_env` without the required env var and asserts it returns `None`.

Run the test suite:

```bash
./scripts/relay-verify.sh
```

---

## Deployment

No redeploy is needed for adapter discovery if Relay is configured to load config from a live source. If the image is rebuilt (`relay-build-hub-image.sh`), the adapter is baked in. Integration credentials are set on the **Settings** screen at runtime — no secret to pre-create at deploy time.

Set the required env vars at deploy time if needed:

```bash
RELAY_<NAME>_TOKEN_SECRET=relay/<name>-token    # Secrets Manager secret name
RELAY_<NAME>_BASE_URL=https://your-instance.com  # optional
```
