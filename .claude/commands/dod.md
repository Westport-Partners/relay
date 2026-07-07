---
description: Run Relay's Definition of Done — automated gates + judgment checklist — and report what's left before this work is shippable.
---

You are running the **Definition of Done (DoD)** check for the Relay repo. Your
job is to determine, rigorously, whether the current change is actually finished
to this project's standards — and to report exactly what remains if not.

The canonical checklist lives in `CONTRIBUTING.md` ("Definition of Done"
section). This command runs the automatable parts and walks the judgment parts.
Do not rubber-stamp: your value is catching the things that are easy to forget
(docs out of sync, status.md not updated, tests missing for new paths).

## Step 1 — Run the automated gates

Run the shared gate script and report its output faithfully:

```
scripts/relay-verify.sh --base main
```

(If the change touched `docs/` or `infra/` the script auto-detects and runs
`mkdocs build --strict` / `relay-synth.sh`. Pass `--all` to force everything.)

Report each gate's result. **Blocking gates** — ruff, `mypy` (src, infra, tools,
tests), pytest, and (when relevant) mkdocs --strict and cdk synth — must all pass.

## Step 2 — Determine what changed

Run `git diff --name-only main...HEAD` and `git status --short` to get the full
list of files this change touches. You will use this to drive the doc and test
checks below — do not rely on memory of what you edited.

## Step 3 — Walk the judgment checklist

These cannot be fully automated. For each, state PASS / NEEDS-ACTION / N/A with a
one-line reason grounded in the actual diff:

1. **Docs ownership.** For every changed file, map it to the docs that must be
   reviewed, using the table in `CONTRIBUTING.md` ("Definition of Done" →
   doc-ownership map). Common cases:
   - `docker-compose.yml`, `scripts/relay-fire.sh`, `fixtures/alarms/*`, demo/test-env tooling → `docs/local-dev.md`
   - HTTP routes in `src/relay/hub/app.py` → `docs/operate.md`
   - env vars read via `os.environ` in `src/relay` → `docs/configure.md`
   - `src/relay/config/schema.py`, `config/*.example.yaml` → `docs/configure.md`, `config/README.md`
   - `infra/stacks/*.py`, deploy scripts → `docs/deploy.md`, `docs/byor.md`, `infra/README.md`
   - `core/scheduling.py`, `core/escalation.py` → `docs/scheduling.md`
   - `adapters/integrations/*`, `adapters/ai/*` → `docs/integrations.md`
   - any user-facing capability/install/contract change → `README.md`
   Check whether those docs were actually updated in this diff. List any that
   still need attention.

2. **`docs/status.md` reconciled — SAME COMMIT.** If this change altered the
   build-state of any feature (new capability, stub→real, partial→done), the
   matching `status.md` row must be updated **in the same commit**: correct
   mark (✅/🟡/🔄/🔬/🗺️/⛔), `file:line` evidence pointing at the real new code,
   "Last verified" bumped, and the top rollup lists (In progress / Researching /
   Roadmap) kept in sync. Verify the row exists AND its evidence path resolves to
   real code. This is the single most-often-missed item — scrutinize it.

3. **New code paths have tests.** For each new/changed behavior, is there a
   corresponding `tests/test_*.py`? Suite-green is necessary but not sufficient —
   a brand-new endpoint/function with no test is NEEDS-ACTION even if pytest passes.

4. **Architecture invariant.** Core stays AWS-free: run
   `grep -rn "import boto3\|from boto3" src/relay/core/` — must be empty.

5. **Secrets / public-artifact hygiene.** Scan the diff for credentials, AWS
   account IDs, tokens, and any government-agency names (hard rule: say
   "government agencies", never name a specific agency). The authoritative gate
   is the `secret-scan` CI workflow (gitleaks), but flag anything suspicious now.

6. **No debug leftovers** you introduced: stray `print(`, `breakpoint()`,
   commented-out blocks, or TODO/FIXME without a tracking note.

7. **Adjacent bugs.** Did you notice anything broken *near* this change that you
   didn't fix? Flag it explicitly (file an issue / mention it) rather than
   silently absorbing or ignoring it.

8. **Spec artifacts archived to the issue.** If this change ships a feature that
   has a `specs/_active/<NNNN-name>/` working set (the gitignored Spec Kit
   artifacts — spec, plan, research, contracts, tasks), those artifacts must be
   attached to the matching GitHub issue #NNNN so the ticket carries the design
   record (they never enter git history). Run
   `scripts/relay-spec-publish.sh <issue> specs/_active/<NNNN-name>` — it
   secret-scans, assembles one collapsible comment, and is idempotent (skips if
   already posted). NEEDS-ACTION if a `specs/_active/` dir for this feature
   exists and the issue has no spec-archive comment yet. N/A for changes with no
   spec dir (small fixes, docs-only, etc.).

9. **Fails loud, not silent.** If the diff touches a delivery / side-effect path
   (paging, SNS/SES publish, ticket create, federation emit, config seed), verify
   that a misconfiguration or permission denial surfaces — a log `error`/`warning`,
   a non-2xx, or a readiness signal — rather than a silent success. A handler that
   returns `{"ok": true}` while delivering nothing (the exact shape of the BYOR
   test-page and unseeded-config bugs) is NEEDS-ACTION. N/A for pure read/UI/docs
   changes.

10. **Off-happy-path considered.** Our sandbox is permissive and our build arch
    usually matches the target, so whole classes of blocker hide until a real
    locked-down / cross-arch / fresh-install deploy. For infra/deploy/IAM/container
    changes, state how the change behaves under BYOR (`iam:CreateRole` /
    `ec2:CreateVpc` denied), a non-x86 build host, and a fresh unseeded install —
    or why those axes don't apply. Prefer an automated guard (a pure `resolve_*`
    unit test, a synth assertion) over prose; where it can't be automated, confirm
    an issue is filed. N/A for pure read/UI/docs changes.

## Step 4 — Git hygiene

- Confirm the work is on a **feature branch**, not committed directly to `main`
  (`git branch --show-current`).
- If a `status.md` update is needed (Step 3.2), confirm it is staged for the
  **same commit** as the code, not a follow-up.

## Step 5 — Report

Print a compact report:
- **Blocking gates:** ✓/✗ per gate (from Step 1).
- **Judgment checklist:** the Step 3–4 items with PASS / NEEDS-ACTION.
- **Verdict:** "DONE — shippable" only if every blocking gate passed and no
  judgment item is NEEDS-ACTION. Otherwise "NOT DONE" with a numbered, ordered
  punch list of exactly what to do next.

Per the repo's "always ship after verify" convention, if the verdict is DONE,
remind the user they can push/open the PR. Do not push automatically — that is
the user's call.
