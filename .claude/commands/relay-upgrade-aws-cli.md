---
description: Upgrade the local AWS CLI to v2.35+ (enables Relay's opt-in CloudFormation Express Mode deploy path).
---

You are helping the user upgrade the AWS CLI (v2). The usual trigger is Relay's
`aws-cli-express` preflight WARN: CloudFormation Express Mode (the opt-in
`RELAY_CFN_MODE=EXPRESS` fast deploy) needs AWS CLI >= 2.35 (`--deployment-config`).
This upgrade is optional — the default `STANDARD` deploy works on any AWS CLI v2.

Read and follow **`prompts/upgrade-aws-cli.md`** in this repo for the exact
detect → upgrade → verify → clean-up steps.

**Relay-specific reminders:**
- Detect first: `aws --version`, `uname -s`, `uname -m`, `which aws`. Pick the
  command by platform/arch — never assume.
- If a v2 is already installed, the Linux installer needs `--update`; a first-time
  or post-v1 install omits it. macOS `.pkg` upgrades in place with no flag.
- If `which aws` is a non-default path (e.g. `~/.local/bin/aws`), pass matching
  `--bin-dir` / `--install-dir` so you update it in place instead of creating a
  second install.
- Verify with `aws cloudformation create-stack help | grep -- '--deployment-config'`
  and `hash -r` if the shell still resolves the old binary.
- This changes only the local CLI — no AWS account state is touched.
