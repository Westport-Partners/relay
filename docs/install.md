# Relay — Install Guide

Relay is a self-hosted AWS incident manager: one always-on container per team,
zero third-party dependencies. This guide covers installing the Relay toolchain
on the machine you deploy from (a laptop, CI runner, or bastion host).

---

## Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/Westport-Partners/relay/main/install.sh | bash
```

For non-interactive use (CI, piped from curl with no TTY):

```bash
curl -fsSL https://raw.githubusercontent.com/Westport-Partners/relay/main/install.sh | bash -s -- --yes
```

The installer prints a summary and runs the preflight checker. When it finishes,
skip to [Next steps](#next-steps).

The one-liner above clones the repo and installs the **deploy toolchain** (CDK +
scripts) on this machine — that is what you need to push the AWS stacks. If you only
want to *run* Relay (try it, or run the Hub process) without a checkout, use a
published artifact instead — see the next section.

---

## Run from a published artifact (no checkout)

Two prebuilt bundles let you run Relay without cloning the repo or installing the CDK
toolchain. Use these to evaluate Relay or run the Hub process; use the
[quick install](#quick-install) above when you're ready to deploy the AWS stacks.

### Container image (GHCR)

The Hub image is published to `ghcr.io/westport-partners/relay` (multi-arch:
`linux/amd64` + `linux/arm64`). The shipped `docker-compose.yml` runs it fully offline
against DynamoDB-Local — no AWS account, no credentials:

```bash
curl -fsSLO https://raw.githubusercontent.com/Westport-Partners/relay/main/docker-compose.yml
docker compose up            # pulls the image, seeds the table, serves the dashboard
open http://localhost:8080/
```

`docker compose up --build` still builds from source for contributors. To pull a
specific release, pin the tag (e.g. `ghcr.io/westport-partners/relay:v0.1.0`) in the
Compose file.

### Python wheel (PyPI)

The `relay-hub`, `relay-node`, and `relay-preflight` console scripts install from PyPI.
The distribution name is **`relay-westport`** (the import package and commands stay
`relay` / `relay-hub`). The `serve` extra pulls in FastAPI/Uvicorn so the Hub can serve
the dashboard:

```bash
pipx install 'relay-westport[serve]'   # isolated, recommended
# or into an existing venv:
pip install 'relay-westport[serve]'

relay-hub --help        # run the Hub
relay-preflight         # run the readiness checker standalone
```

Without `[serve]`, the core package installs (boto3/pydantic) but the Hub cannot serve
HTTP — install the extra for anything beyond library use.

> **Note:** both artifacts are published by CI on a `v*` release tag. The container
> image must also be made public once in the repo's **Packages** settings; the PyPI
> project is created on the first publish via Trusted Publishing.

---

## What the installer does

The installer runs six steps:

| Step | What happens |
|------|-------------|
| 1 | Detects OS/arch and package manager (`apt-get`, `dnf`, `yum`, `apk`, `pacman`) |
| 2 | Unless `--no-deps`: installs baseline tooling — git, curl, unzip, Docker, Node.js ≥ 18, Python ≥ 3.12, AWS CLI v2 |
| 3 | Clones `https://github.com/Westport-Partners/relay.git` into `$RELAY_HOME` (default `~/relay`). If the directory already contains the Relay repo it fetches and checks out the target ref instead of re-cloning |
| 4 | Creates a `.venv` inside `$RELAY_HOME` and runs `pip install -e .` |
| 5 | Seeds `$RELAY_CONFIG_DIR` (default `~/.relay/config`) by copying `escalation.example.yaml` and `routing.example.yaml` to their non-example names. Existing files are never overwritten |
| 6 | Runs `scripts/relay-preflight.sh` — a read-only readiness checker. WARN findings are printed but do not abort the install; only FAIL findings exit non-zero |

---

## Flags

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--dir <path>` | `RELAY_HOME` | `~/relay` | Where to clone the repo |
| `--ref <git-ref>` | — | `main` | Branch, tag, or SHA to check out |
| `--config-dir <path>` | `RELAY_CONFIG_DIR` | `~/.relay/config` | Where to seed live config files |
| `--no-deps` | — | off | Skip tooling install |
| `--yes` / `-y` | — | off | Non-interactive; skip consent prompts. Required when piped from curl |
| `--help` | — | — | Print usage and exit |

---

## Prerequisites

The installer handles these automatically. For a manual install, or if you pass
`--no-deps`, ensure the following are present before deploying:

- Linux x86\_64 or aarch64/arm64 (the only supported platforms)
- AWS credentials for the target account (env vars, `~/.aws/credentials`, or an instance role)
- Node.js 18+
- Python 3.12+
- Docker (daemon running)
- AWS CLI v2

---

## Manual install

If you prefer to skip the one-liner:

```bash
git clone https://github.com/Westport-Partners/relay.git
cd relay
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Then seed your config and run preflight yourself:

```bash
mkdir -p ~/.relay/config
cp config/escalation.example.yaml ~/.relay/config/escalation.yaml
cp config/routing.example.yaml    ~/.relay/config/routing.yaml
./scripts/relay-preflight.sh
```

---

## Preflight check

`scripts/relay-preflight.sh` is a read-only readiness checker with no side effects.
Run it any time to verify the environment before or after a change.

```bash
./scripts/relay-preflight.sh          # human-readable table
./scripts/relay-preflight.sh --json   # JSON (for CI or scripting)
```

**What it checks:**

| Category | Checks |
|----------|--------|
| Tooling | bash ≥ 4, git, AWS CLI v2, Docker daemon reachable, Node.js ≥ 18, Python ≥ 3.12 |
| AWS identity | `sts:GetCallerIdentity` succeeds; region is resolved |
| IAM capability | `iam:CreateRole` and `ec2:CreateVpc` — WARN (not FAIL) if denied; signals you need BYOR mode |
| CDK bootstrap | `CDKToolkit` stack present in the resolved region — WARN if missing |

**Exit codes:** `0` = ready to deploy (no FAILs). `1` = at least one FAIL must be
fixed before deploying. WARN findings print but do not affect the exit code.

---

## Updating an existing install

`scripts/relay-update.sh` updates the clone in place — it does **not** re-clone.

```bash
./scripts/relay-update.sh                   # update to latest on the tracked branch
./scripts/relay-update.sh --ref v1.2.0      # pin to a tag
./scripts/relay-update.sh --no-deps         # skip pip re-install
./scripts/relay-update.sh --force           # allow update with uncommitted changes
```

The updater:
1. Refuses if the working tree has uncommitted changes (unless `--force`)
2. Fetches and checks out the target ref
3. Re-installs the Python package into `.venv` (unless `--no-deps`)
4. Runs a config-drift check — compares top-level keys in your live config against
   the updated templates and highlights anything new that you may want to adopt
5. Re-runs preflight

After updating, rebuild the container image and redeploy. See [deploy.md](deploy.md).

---

## Next steps

1. **Edit your config** in `~/.relay/config` (or the path you chose with `--config-dir`):
   - `escalation.yaml` — escalation policies (who gets paged, by role)
   - `routing.yaml` — alarm-to-policy routing rules

2. **Deploy** — synthesize and push the CDK stacks to AWS:

   ```bash
   # team topology: Node + local Hub in one account
   RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=<team> ./scripts/relay-synth.sh
   RELAY_DEPLOY_TYPE=team RELAY_TEAM_NAME=<team> ./scripts/relay-deploy.sh
   ```

   See [deploy.md](deploy.md) for topology options, BYOR mode, and rollout details.
