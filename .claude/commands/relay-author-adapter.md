---
description: Add a new Relay integration adapter — copy the template, implement sink + listener + manifest, register, and test.
---

You are helping the user add a new external integration adapter to Relay following the auto-discovery convention. The task covers copying `src/relay/adapters/integrations/_template/` to a new named folder, implementing `sink.py` (external client + `from_env` factory), `listener.py` (lifecycle event handler), and `adapter.py` (MANIFEST), then writing tests.

Read and follow **`prompts/author-adapter.md`** in this repo for the exact interface, code structure, and testing requirements.

**Relay-specific reminders:**
- `build()` must return `None` when the integration is not configured — a missing token never blocks startup or paging.
- Catch all exceptions in `create_record` and `close_record` and return `""` / `None` on failure. A broken adapter must never delay paging or block other listeners.
- Never import `boto3` into `src/relay/core/` — keep all cloud SDK calls in the adapter (behind `sink.py`).
- Credentials are read via `secret_fetcher` (injected by `AdapterContext`) and env vars only. Never hardcode tokens. No PII in Git.
- The folder name must not start with `_` — the `_template` prefix causes the registry to skip the original. Your new folder (without underscore) is auto-discovered.
