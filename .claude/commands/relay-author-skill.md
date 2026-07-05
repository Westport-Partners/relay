---
description: Add a new Relay AI investigation skill — write a read-only probe.sh and a SKILL.md with frontmatter, inputs, and interpretation guide.
---

You are helping the user add a new runtime AI investigation skill pack to the `skills/` directory. Each skill is a directory with a `SKILL.md` (frontmatter + when-to-use + inputs + probe invocation + interpretation guide) and a read-only `probe.sh` (bash script wrapping AWS CLI describe/list/get calls). Skills are mounted into the node's headless AI triage agent at incident time.

Read and follow **`prompts/author-skill.md`** in this repo for the exact structure, script skeleton, and verification steps.

**Relay-specific reminders:**
- `probe.sh` must be **strictly read-only**: only `describe*` / `list*` / `get*` / `lookup-events` / `filter-log-events` AWS CLI calls. Never a mutating call. Verify with `grep` for create/update/delete/put/start/stop patterns.
- Each section of the probe must be independently wrapped so one failing section does not abort the rest — degrade gracefully.
- The `SKILL.md` interpretation section must frame findings as hypotheses with evidence, never as confirmed root causes. The human decides.
- Use `RELAY_WINDOW_MINUTES` (default 60) for all time-range parameters — never fan out to unbounded time ranges.
- Skills probe infrastructure state only — no PII in probe output or in `SKILL.md` examples.
