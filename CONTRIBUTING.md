# Contributing to Relay

Thanks for your interest in Relay — an open-source AWS Incident Manager replacement by
[Westport Partners](https://www.westportpartners.com/). Contributions are welcome.

## Ground rules

- Be respectful — see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- By contributing, you agree your contributions are licensed under
  [Apache-2.0](LICENSE) (the project license). No CLA is required.
- Keep the trademark/branding boundaries in mind — see [TRADEMARK.md](TRADEMARK.md).

## Development setup

```bash
git clone https://github.com/Westport-Partners/relay.git
cd relay
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # run the test suite
```

The codebase is Python 3.12. Core domain logic in `src/relay/core/` is **AWS-free**;
all AWS/SaaS specifics live behind adapter interfaces in `src/relay/adapters/`. Please
preserve that separation — no `boto3` imports in `core/`.

## Making changes

1. Open an issue first for anything non-trivial, so we can align on approach.
2. Branch from `main`, make focused commits.
3. **Add/Update tests.** The suite must stay green (`pytest -q`).
4. Run the gates — `scripts/relay-verify.sh` bundles `ruff check`, `mypy`
   (advisory), `pytest`, and the conditional docs/infra checks. See
   [Definition of Done](#definition-of-done) below for the full list.
5. For infrastructure changes (`infra/`), confirm `cdk synth` succeeds for the affected
   stacks.
6. Open a PR with a clear description of the change and why. Link the issue.

## Definition of Done

A change is "done" only when the items below hold. Most of the automatable ones
are bundled into one script so you don't have to remember them:

```bash
scripts/relay-verify.sh          # runs ruff, mypy (advisory), pytest, and —
                                 # when docs/ or infra/ changed — mkdocs --strict
                                 # and cdk synth. Exits non-zero on any blocking failure.
```

Claude Code users can run the **`/dod`** slash command, which runs that script
and then walks the judgment items below against the actual diff.

**Blocking — must pass:**

- [ ] **Lint:** `ruff check .` clean (CI runs this).
- [ ] **Tests:** `pytest -q` green, and new/changed behavior has tests (a green
      suite with an untested new code path is *not* done).
- [ ] **Docs site (if `docs/` or `mkdocs.yml` changed):** `mkdocs build --strict`
      succeeds — no broken nav or links.
- [ ] **Infra (if `infra/` or deploy scripts changed):** `cdk synth` succeeds
      (`scripts/relay-synth.sh`, no AWS writes).
- [ ] **Docs ownership applied:** for every file you touched, the owning doc in
      the table under [Maintaining the docs](#maintaining-the-docs) is updated
      in the **same PR/commit**.
- [ ] **`docs/status.md` reconciled — same commit:** if the change altered any
      feature's build-state, its row is updated with the correct mark, real
      `file:line` evidence, a bumped "Last verified", and the top rollup lists
      kept in sync. *(This is the most commonly missed item.)*
- [ ] **Core stays AWS-free:** no `boto3` import under `src/relay/core/`.
- [ ] **No secrets / account IDs / agency names** in the diff (say "government
      agencies", never a specific agency). Backstopped by the `secret-scan` CI job.
- [ ] **Feature branch**, focused commits — not committed directly to `main`.

**Advisory — report, don't block (yet):**

- [ ] **Types:** `mypy src` (config in `pyproject.toml`). There is a known
      backlog of pre-existing strict errors; the goal is to add no *new* ones and
      drive the backlog to zero, after which this becomes blocking in CI.
- [ ] **Adjacent bugs:** anything broken near your change that you didn't fix is
      filed or flagged, not silently absorbed.

## What makes a good PR

- Matches the surrounding code style, comment density, and naming.
- Small and reviewable. Split unrelated changes.
- Updates docs (`docs/`, `README.md`) when behavior or interfaces change.
- No secrets, credentials, or account-specific values committed.

## Maintaining the docs

The docs rotted once before because they narrated implementation internals (which Lambda, which
bus hop, file-level mechanics) that changed with every refactor. The rewrite is built on one
principle:

> **Document contracts, not internals.** Write about the things a user or operator depends on —
> the installer one-liner and its flags, the deploy scripts and their env vars, the CDK context
> keys, the HTTP endpoints, the config-file shape, the severity tiers. Keep volatile `file:line`
> evidence out of user docs; it belongs only in `docs/status.md`, which is explicitly the
> code-verified feature ledger and carries its own "re-verify against code" discipline.

**The rule:** documentation edits land in the *same PR* as the behavior change. If your change
alters a contract below, update the owning doc before you open the PR.

**Doc ownership — when you change X, review doc Y:**

| When you change… | Review / update… |
|---|---|
| `install.sh`, `scripts/relay-preflight.sh`, `scripts/relay-update.sh` | `docs/install.md` |
| `scripts/relay-*.sh` deploy flow, `relay-context.sh` env vars | `docs/deploy.md` |
| `infra/stacks/*.py` (stack names, context keys, outputs, BYOR/BYOV) | `docs/deploy.md`, `docs/byor.md`, `infra/README.md` |
| Env vars the container reads (`os.environ` / `os.getenv` in `src/relay`) | `docs/configure.md` |
| `src/relay/config/schema.py`, `config/*.example.yaml` | `docs/configure.md`, `config/README.md` |
| HTTP routes in `src/relay/hub/app.py` | `docs/operate.md` |
| `core/scheduling.py`, `core/escalation.py` (the on-call / escalation model) | `docs/scheduling.md` |
| `adapters/integrations/*`, AI providers in `adapters/ai/*` | `docs/integrations.md` |
| `docker-compose.yml`, `scripts/relay-fire.sh`, `fixtures/alarms/*` | `docs/local-dev.md` |
| The incident flow / topology / stack shape | `docs/architecture.md` |
| **Any feature's build state** (done / partial / roadmap) | `docs/status.md` — **same commit**, with updated evidence and a bumped "Last verified" date |

`coverage.md` (the comparison vs. AWS Incident Manager) and `vision.md` are direction documents;
touch them only when scope or positioning actually changes.

**Before opening a docs PR**, build the site to catch broken nav/links:

```bash
pip install -r requirements-docs.txt
mkdocs build --strict
```

## Reporting bugs / requesting features

Use the GitHub issue templates. For security issues, **do not** open a public issue —
see [SECURITY.md](SECURITY.md).
