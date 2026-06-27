# Domain Spec: Post-Incident Analysis

**Owns:** generating the After-Action Report (AAR) for a resolved incident —
an AI-drafted narrative with a deterministic fallback.

**Primary code:** `core/analysis.py` (`generate_aar`, `_fallback_aar`),
`hub/app.py` (`GET /incidents/{id}/aar`).
**status.md:** §7. **Related domains:**
[incident-records](../incident-records/spec.md) (timeline + properties used as
AAR input), [ai](../ai/spec.md) (provider called by `generate_aar`),
[ui](../ui/spec.md) (AAR surface in the incident drawer).

## What it does now

- **`generate_aar`** produces a written AAR from the incident's timeline and
  properties. When an AI provider is configured and reachable it drafts the
  narrative via the pluggable AI adapter; when AI is off or unavailable it falls
  back to `_fallback_aar`, a deterministic markdown summary built from the
  timeline.
- **`GET /incidents/{id}/aar`** serves the report on demand; no background
  pre-generation.
- Output is **markdown only** — no PDF rendering.

## Key entities

- **`generate_aar(incident, ai_assistant)`** — pure function; takes an
  `Incident` and an optional `AIAssistant`; returns markdown text.
- **`_fallback_aar`** — deterministic fallback; timeline-derived markdown,
  never raises.

## Invariants

- **AWS-free core:** `core/analysis.py` contains no `boto3` or HTTP calls;
  all AI I/O is behind the `AIAssistant` interface from `adapters/ai/`.
- **Fallback is always safe:** `generate_aar` must return valid markdown even
  when `ai_assistant` is `None` or raises.

## Out of scope (non-goals)

- **PDF export** of the AAR — markdown only today; PDF is roadmap
  (status.md §7 🗺️).
