# Relay — Upgrade the AWS CLI Prompt

You are helping the user upgrade the AWS CLI (v2) to a version new enough for
Relay's optional features. Work carefully: detect the current version and the
platform first, then run the official installer in the right mode (fresh install
vs. in-place update), then verify.

## Why a user is here

The most common reason is **CloudFormation Express Mode** — the opt-in
`RELAY_CFN_MODE=EXPRESS` fast path in `scripts/relay-deploy-direct.sh`. It calls
`aws cloudformation create-stack`/`update-stack` with `--deployment-config`, which
was added in **AWS CLI 2.35**. `scripts/relay-preflight.sh` surfaces this as a
WARN (`aws-cli-express`) when the installed CLI predates 2.35. Express Mode is
**optional** — the default `STANDARD` deploy works on any AWS CLI v2, so this
upgrade is a convenience, never a hard requirement.

Only AWS CLI **v2** is supported. If the user is on v1, this same procedure moves
them to v2 (there is no in-place v1→v2 upgrade; the v2 installer replaces it).

---

## Step 1 — Detect current state

```bash
aws --version 2>&1 || echo "aws CLI not found"
uname -s   # Linux | Darwin (macOS)
uname -m   # x86_64 | aarch64 | arm64
which aws  # note the install location — the installer must target it
```

Read the version from the `aws-cli/<major>.<minor>.<patch>` field. Decide:

- **≥ 2.35** — already Express-capable. No upgrade needed; stop here.
- **2.0 – 2.34** — v2 but pre-Express. Use the **`--update`** installer below.
- **1.x or not found** — install v2 fresh (omit `--update`).

Do not assume the platform — always confirm from `uname` before picking a command.

---

## Step 2 — Upgrade (Linux)

The AWS CLI v2 on Linux is distributed as an official zip installer, not via the
system package manager. Pick the URL by architecture from Step 1.

**x86_64:**

```bash
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
unzip -o /tmp/awscliv2.zip -d /tmp
sudo /tmp/aws/install --update    # omit --update only for a first-time install
```

**aarch64 / arm64:**

```bash
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o /tmp/awscliv2.zip
unzip -o /tmp/awscliv2.zip -d /tmp
sudo /tmp/aws/install --update
```

> **`--update` is required when a v2 is already installed.** Without it the
> installer aborts with "Found preexisting AWS CLI installation … Please rerun
> install script with --update flag." For a first-time (or post-v1) install,
> omit `--update`.

> **Non-default install location.** If `which aws` in Step 1 pointed somewhere
> other than `/usr/local/bin/aws` (e.g. a user-local `~/.local/bin/aws` →
> `~/.local/aws-cli/...`), pass matching `--bin-dir` and `--install-dir` so you
> update the existing install rather than creating a second one:
>
> ```bash
> sudo /tmp/aws/install --update \
>   --bin-dir "$HOME/.local/bin" \
>   --install-dir "$HOME/.local/aws-cli"
> ```
> (Drop `sudo` when installing under `$HOME`.)

---

## Step 2 (alt) — Upgrade (macOS)

```bash
curl -fsSL https://awscli.amazonaws.com/AWSCLIV2.pkg -o /tmp/AWSCLIV2.pkg
sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
```

The macOS `.pkg` installer upgrades in place automatically — no `--update` flag.

---

## Step 3 — Verify

```bash
hash -r                         # clear the shell's cached path to the old binary
aws --version                   # expect aws-cli/2.35.x or newer
# Confirm the Express Mode flag is present:
aws cloudformation create-stack help 2>/dev/null | grep -q -- '--deployment-config' \
  && echo "Express Mode: SUPPORTED" \
  || echo "Express Mode: STILL MISSING (version too old, or PATH still points at the old binary)"
```

If it still reports the old version, the shell is resolving a stale binary. Open a
new shell, or re-check `which aws` and confirm you updated *that* location.

Optionally re-run preflight to see the `aws-cli-express` check flip to PASS:

```bash
./scripts/relay-preflight.sh --json 2>/dev/null | \
  python3 -c "import sys,json; [print(c['status'],c['name']) for c in json.load(sys.stdin)['checks'] if c['name'].startswith('aws-cli')]"
```

---

## Step 4 — Clean up

```bash
rm -rf /tmp/aws /tmp/awscliv2.zip /tmp/AWSCLIV2.pkg
```

---

## Notes

- This only affects the **local** AWS CLI. Nothing in AWS changes; no credentials
  or account state is touched.
- If the user cannot run `sudo` / cannot modify the system install (locked-down
  workstation), they can install the CLI under `$HOME` with the `--bin-dir` /
  `--install-dir` form above and put that `bin` dir first on `PATH` — no root
  needed. Flag this as the fallback rather than asking them to escalate.
- After upgrading, Express Mode is still **opt-in** — the user must set
  `RELAY_CFN_MODE=EXPRESS` on the deploy. See [`deploy-byor.md`](deploy-byor.md).
