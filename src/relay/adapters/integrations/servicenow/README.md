# ServiceNow adapter

Creates a ServiceNow **incident record** (Table API) when an incident triggers and
closes it on resolve.

## Lifecycle events handled

| Event | Action |
|---|---|
| `TRIGGERED` | `POST` a new incident to the Table API; stamp `incident.servicenow_sys_id`. |
| `RESOLVED` | Close the record (state → resolved) with a close note. |

## Configuration (environment)

| Var | Required | Purpose |
|---|---|---|
| `RELAY_SERVICENOW_INSTANCE_URL` | yes | e.g. `https://yourinstance.service-now.com`. |
| `RELAY_SERVICENOW_USERNAME` | yes | API user. |
| `RELAY_SERVICENOW_SECRET` | yes | Secrets Manager secret *name* holding the API password. |

The adapter is enabled only when the instance URL **and** a resolved password are
present; otherwise `build()` returns `None` and the Hub runs without it.

## Files
- `sink.py` — `ServiceNowSink` (Table API client), `ServiceNowConfig`, `from_env`.
- `listener.py` — `ServiceNowListener` (events → sink calls).
- `adapter.py` — `MANIFEST` + `build(ctx)`.
