# Domain Spec: AI Capability

**Owns:** the pluggable AI provider layer and the incident investigation features
built on top of it — the t=0 briefing pack and the (future) multi-step agent
loop.

**Primary code:** `adapters/ai/factory.py` (`make_assistant`, `RELAY_AI_PROVIDER`),
`adapters/ai/bedrock_assistant.py`, `adapters/ai/bedrock_converse.py`,
`adapters/ai/openai_compat.py`, `adapters/ai/claude_code_assistant.py`,
`adapters/base.py` (`AICompletion`),
`hub/app.py` (`_attach_ai_brief`, `GET /incidents/{id}/brief`).
**status.md:** §14. **Related domains:**
[incident-records](../incident-records/spec.md) (incident + timeline used as AI
input; `ai.brief` timeline event), [post-incident](../post-incident/spec.md)
(AAR generation calls the same AI interface),
[integrations-config](../integrations-config/spec.md) (`AIBriefListener`
subscribes to the lifecycle seam),
[observability](../observability/spec.md) (`ai.brief` timeline event).

## What it does now

- **Pluggable provider factory** (`make_assistant`): selects the AI backend from
  `RELAY_AI_PROVIDER`. Supported providers:
  - **Bedrock** (`bedrock_assistant.py`) — default; `invoke_model` API.
  - **Bedrock Converse** (`bedrock_converse.py`) — any Bedrock model via the
    unified Converse schema.
  - **OpenAI-compatible** (`openai_compat.py`) — base URL + API key from
    Secrets Manager; covers OpenAI, Azure, Gemini, local models, OpenRouter.
  - **Claude Code** (`claude_code_assistant.py`) — shells out to the headless
    `claude` CLI with a read-only allow-list; graceful degradation when the CLI
    is unavailable.
- **`AICompletion` result type** (`adapters/base.py`): `{ text, model, tokens,
  provider }`; uniform across providers; enables cost/usage telemetry.
- **t=0 AI briefing pack** (`_attach_ai_brief`): on `TRIGGERED`, the Hub
  asynchronously generates a brief from the incident's initial context and
  attaches it as an `ai.brief` `TimelineEvent`. Never gates paging — runs in
  the background after the incident is persisted and paging is dispatched.
  Accessible via `GET /incidents/{id}/brief`.

## Key entities

- **`AIAssistant`** — abstract interface in `adapters/base.py`; all providers
  implement it.
- **`AICompletion`** — `{ text, model, tokens, provider }`.
- **`make_assistant(provider)`** — factory; returns the configured `AIAssistant`
  or `None` when AI is disabled.
- **`_attach_ai_brief`** — Hub-side async brief generation; appends `ai.brief`
  timeline event.

## Invariants

- **AI never gates paging** — `_attach_ai_brief` runs asynchronously after
  paging is dispatched; a slow or failing AI call must not delay notification.
- **Graceful degradation** — when the AI provider is unavailable or `None`, all
  AI-dependent features fall back silently (brief is absent; AAR uses
  `_fallback_aar`).
- **Anthropic-direct is not a supported provider** — Bedrock (default) and the
  OpenAI-compatible umbrella cover the need; direct Anthropic API access is out
  of scope.
- **`claude_code_assistant` is read-only** — the allow-list passed to the headless
  CLI restricts it to read-only operations; no write actions from AI.

## Out of scope (non-goals)

- **AI investigator Tier 2/3 agent loop** — the interface, briefing slice, and
  `claude_code` adapter exist; the live multi-step investigation loop (Claude Code
  skills doing real account investigation) is future work (status.md §14 🔬).
- **Anthropic-direct provider** — out of scope; Bedrock is the default AWS-native
  path (status.md §14 ⛔).
