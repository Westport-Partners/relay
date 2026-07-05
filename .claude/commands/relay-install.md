---
description: Install the Relay toolchain, run preflight, and interpret results — choose between the one-liner, manual, PyPI wheel, and GHCR image paths.
---

You are helping the user install the Relay deploy toolchain and verify readiness. The task covers the quick one-liner install, the manual path, the PyPI wheel, the GHCR container image (for offline evaluation), running `scripts/relay-preflight.sh`, and interpreting WARN vs. FAIL findings.

Read and follow **`prompts/install.md`** in this repo for the exact steps, commands, flags, and how to interpret every preflight finding.

**Relay-specific reminders:**
- WARN on `iam:CreateRole` or `ec2:CreateVpc` does not block install — it signals the user needs BYOR mode for the compute stack. Direct them to `prompts/deploy-byor.md` before deploying.
- WARN on CDK bootstrap missing is resolved by running `scripts/relay-bootstrap.sh` before the first deploy.
- Only FAIL findings require action before deploying; WARN findings are informational.
- Do not create, modify, or delete any AWS resources during install — `relay-preflight.sh` is read-only.
