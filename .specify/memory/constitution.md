# Relay Constitution

This is a **pointer, not a fork.** Relay already has a complete rule set; this
file tells the spec-kit workflow where it lives. Do not restate or duplicate the
rules below — read the source documents. When they change, this file does not.

## Authoritative sources (read these)

- **`CLAUDE.md`** (user-global, `~/.claude/CLAUDE.md` — not in-repo) — agent
  behavior + multi-model delegation routing (Opus orchestrates, Sonnet codes,
  Haiku scans).
- **[`CONTRIBUTING.md`](../../CONTRIBUTING.md)** → "Definition of Done" — the
  binding quality gates and the doc-ownership map (the in-repo source of truth).
- **[`docs/status.md`](../../docs/status.md)** — the code-verified feature ledger
  (the only source of truth for "what state is feature X in").
- **[`specs/DOMAIN-MAP.md`](../../specs/DOMAIN-MAP.md)** — how the app is laid out;
  which domain owns what; the issues-vs-spec-vs-code churn discipline.
- **[`specs/ui/design-language.md`](../../specs/ui/design-language.md)** — binding
  visual constitution for all UI (Industrial Command Center + Westport brand).

## Non-negotiable principles (summarized — sources above are binding)

1. **AWS-free core.** No `boto3` under `src/relay/core/`; AWS specifics live behind adapters.
2. **No agency names** in any code/doc/spec/commit — say "government agencies."
3. **No secrets / account IDs** in the repo (backed by the `secret-scan` CI gate).
4. **Docs in the same PR.** A contract change updates its owning doc and the
   `docs/status.md` row in the *same commit* (DoD).
5. **Specs describe the present, clean.** Iteration history lives in GitHub issues;
   `specs/<domain>/spec.md` is rewritten clean each change, never appended to.
6. **GitHub issues are the canonical task tracker.** spec-kit `tasks.md` is a
   transient per-feature scratchpad, not a second source of truth.
7. **DoD terminates every implementation.** `/speckit-implement` is not done until
   `scripts/relay-verify.sh` + `/dod` pass. UI changes must be exercised in a browser.

## Workflow mapping (spec-kit → Relay)

| spec-kit phase | Relay |
|---|---|
| constitution | this file (pointer) |
| `/speckit-specify` | the durable per-domain `specs/<domain>/spec.md` |
| `/speckit-plan` | technical strategy — orchestrator (Opus) |
| `/speckit-tasks` | ordered work — but **GitHub issues stay canonical** |
| `/speckit-implement` | implement → `scripts/relay-verify.sh` + `/dod` |

## Governance

These rules supersede spec-kit defaults where they conflict. The authoritative
sources above win over any summary here. Amend by editing the source document,
not this pointer.
