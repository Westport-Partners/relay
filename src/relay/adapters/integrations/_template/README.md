# `<name>` adapter (template)

> Skeleton. Copy this folder to `src/relay/adapters/<name>/` (drop the leading
> underscore so the registry discovers it), then replace `Template`/`template`
> throughout. See [`../README.md`](../README.md) for the full contract.

One-line description of what this adapter does.

## Lifecycle events handled

| Event | Action |
|---|---|
| `TRIGGERED` | Create an external record; record a `<name>.ticket_created` timeline event. |
| `RESOLVED` | Close the external record. |

## Configuration (environment)

| Var | Required | Purpose |
|---|---|---|
| `RELAY_TEMPLATE_TOKEN_SECRET` | yes | Secrets Manager secret *name* for the API token. |
| `RELAY_TEMPLATE_BASE_URL` | no | Instance base URL. |

The adapter is enabled only when configured; otherwise `build()` returns `None`
and the Hub runs without it.

## Required scopes / permissions

Document the minimum token scope / role needed to create and close records.

## Files
- `sink.py` — external client + `from_env`.
- `listener.py` — events → sink calls.
- `adapter.py` — `MANIFEST` + `build(ctx)`.
