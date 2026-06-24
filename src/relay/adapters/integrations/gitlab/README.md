# GitLab adapter

Opens a GitLab **incident-type issue** when an incident triggers and closes it on
resolve. Issues are tagged for GitLab **DORA** (time-to-restore, change-failure-rate).

## Lifecycle events handled

| Event | Action |
|---|---|
| `TRIGGERED` | Create an `issue_type=incident` issue in the resolved project; stamp `incident.gitlab_iid`. |
| `RESOLVED` | Close the issue (and add a closing note). |

## Configuration

### Environment (Secrets Manager fallback)
| Var | Required | Purpose |
|---|---|---|
| `RELAY_GITLAB_TOKEN_SECRET` | one of token sources | Secrets Manager secret *name* holding the token. |
| `RELAY_GITLAB_PROJECT_ID` | no | Optional fallback project; normally resolved per incident from the catalog. |
| `RELAY_GITLAB_BASE_URL` | no | GitLab instance base URL (default `https://gitlab.com`). |
| `RELAY_GITLAB_ENV_TIER_MAP` | no | `relay_env:gitlab_tier` pairs for DORA, e.g. `prod:production,staging:staging`. |

### Settings store (UI-set, takes precedence)
| Key | Purpose |
|---|---|
| `gitlab_token` (`SettingsKey.GITLAB_TOKEN`) | UI-set token; **overrides** the Secrets Manager token, resolved live per request. |

The adapter is enabled when **either** a Secrets Manager token **or** a UI token is
present; otherwise `build()` returns `None` and the Hub simply runs without it.

### Token scopes
Use a **project** or **group access token** (or a PAT) with the **`api`** scope and
at least the **Reporter** role on the target projects. `read_api` is insufficient —
`api` is required to create, label, and close incident-type issues.

## Per-incident project resolution
The project is resolved from the catalog/org tree by `deployment_id` (the leaf
node's `gitlab_project`, URL-encoded into the API path). A token-bound sink files
into whichever project owns the failing deployment.

## Files
- `sink.py` — `GitLabSink` (HTTP client), `GitLabConfig`, `from_env`, `test_token`.
- `listener.py` — `GitLabListener` (events → sink calls).
- `adapter.py` — `MANIFEST` + `build(ctx)` (the registry plug point).
