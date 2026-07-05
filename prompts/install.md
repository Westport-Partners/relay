# Relay — Install Prompt

You are helping the user install the Relay toolchain on a Linux machine (x86_64 or aarch64/arm64). Relay runs as a single ECS Fargate container deployed via AWS CDK. This prompt covers installing the deploy toolchain, choosing the right artifact for the use case, running the preflight checker, and interpreting its results.

Canonical reference: [`docs/install.md`](../docs/install.md).

---

## Goal

Get the deploy toolchain installed and preflight passing (exit 0) so the user is ready to run a deploy.

## Preconditions

Before starting, confirm:
- The target machine is Linux x86_64 or aarch64/arm64.
- AWS credentials are available (env vars, `~/.aws/credentials`, or an instance role).
- The user has decided which path they want (see "Choose your path" below).

---

## Choose your path

### A. Quick install (one-liner)

The standard path. Clones the repo, installs toolchain dependencies (git, curl, Docker, Node.js ≥ 18, Python ≥ 3.12, AWS CLI v2), creates a `.venv`, and seeds example config files.

```bash
curl -fsSL https://raw.githubusercontent.com/Westport-Partners/relay/main/install.sh | bash
```

For CI or non-interactive use (no TTY):

```bash
curl -fsSL https://raw.githubusercontent.com/Westport-Partners/relay/main/install.sh | bash -s -- --yes
```

Installer flags:

| Flag | Env var | Default | Purpose |
|------|---------|---------|---------|
| `--dir <path>` | `RELAY_HOME` | `~/relay` | Clone destination |
| `--ref <git-ref>` | — | `main` | Branch, tag, or SHA |
| `--config-dir <path>` | `RELAY_CONFIG_DIR` | `~/.relay/config` | Where example configs are seeded |
| `--no-deps` | — | off | Skip toolchain install (deps already present) |
| `--yes` / `-y` | — | off | Non-interactive |

After the installer finishes, it runs `scripts/relay-preflight.sh` automatically. Skip to "Reading preflight output" below.

### B. Manual install (no one-liner)

Use when the user wants to audit every step or work from an existing checkout:

```bash
git clone https://github.com/Westport-Partners/relay.git
cd relay
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .

# Seed example configs (skip if already present)
mkdir -p ~/.relay/config
cp config/escalation.example.yaml ~/.relay/config/escalation.yaml
cp config/routing.example.yaml    ~/.relay/config/routing.yaml

./scripts/relay-preflight.sh
```

### C. Run from a published artifact (no checkout)

Use this when the user only wants to evaluate Relay or run the Hub process without deploying AWS stacks.

**Container image (GHCR) — offline demo, no AWS account required:**

```bash
curl -fsSLO https://raw.githubusercontent.com/Westport-Partners/relay/main/docker-compose.yml
docker compose up       # pulls ghcr.io/westport-partners/relay, seeds DynamoDB-Local
open http://localhost:8080/
```

To pin a specific release, edit the image tag (e.g. `ghcr.io/westport-partners/relay:v0.1.0`) in the Compose file.

**PyPI wheel — run Relay commands without a repo clone:**

```bash
pipx install 'relay-westport[serve]'   # isolated, recommended
# or:
pip install 'relay-westport[serve]'

relay-hub --help
relay-preflight        # standalone preflight checker
```

The `[serve]` extra adds FastAPI/Uvicorn so the Hub can serve the dashboard. Without it only the core package installs.

> Use paths A or B when you are ready to deploy the AWS stacks. Paths C are for evaluation and local runs only.

---

## Running preflight manually

`scripts/relay-preflight.sh` is a read-only readiness checker with no side effects. Run it any time:

```bash
./scripts/relay-preflight.sh          # human-readable table
./scripts/relay-preflight.sh --json   # JSON output for CI
```

**What it checks:**

| Category | Checks |
|----------|--------|
| Tooling | bash ≥ 4, git, AWS CLI v2, Docker daemon reachable, Node.js ≥ 18, Python ≥ 3.12 |
| AWS identity | `sts:GetCallerIdentity` succeeds; region is resolved |
| IAM capability | `iam:CreateRole` and `ec2:CreateVpc` — WARN if denied |
| CDK bootstrap | `CDKToolkit` stack present in the resolved region — WARN if missing |

---

## Reading preflight output

**Exit codes:** `0` = ready (no FAILs). `1` = at least one FAIL must be fixed.

**FAIL findings** must be fixed before deploying:
- Missing tooling → install the listed tool.
- `sts:GetCallerIdentity` fails → AWS credentials are not configured. Set `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, or log in via SSO.
- Region not resolved → set `AWS_REGION` or `AWS_DEFAULT_REGION`.

**WARN findings** do not block the install but signal a path choice:
- `iam:CreateRole` denied → the user needs BYOR mode for the compute stack. Tell them to follow [`prompts/deploy-byor.md`](deploy-byor.md).
- `ec2:CreateVpc` denied → the user needs BYOV mode. Same file.
- CDK bootstrap missing → remind the user to run `./scripts/relay-bootstrap.sh` before deploying. This is covered in [`prompts/deploy-team.md`](deploy-team.md).

---

## Updating an existing install

```bash
./scripts/relay-update.sh                   # update to latest on tracked branch
./scripts/relay-update.sh --ref v1.2.0      # pin to a tag
./scripts/relay-update.sh --no-deps         # skip pip re-install
./scripts/relay-update.sh --force           # allow update with uncommitted changes
```

The updater refuses if the working tree has uncommitted changes (unless `--force`), fetches the target ref, re-installs the Python package, runs a config-drift check, and re-runs preflight. After updating, rebuild the container image and redeploy — see [`prompts/deploy-team.md`](deploy-team.md).

---

## Next steps

Once preflight exits 0:
- Standard deploy → [`prompts/deploy-team.md`](deploy-team.md)
- Locked-down account → [`prompts/deploy-byor.md`](deploy-byor.md)
- Org-wide hub → [`prompts/deploy-federated-hub.md`](deploy-federated-hub.md)
