#!/usr/bin/env bash
# relay-bootstrap.sh — ensure CDK is bootstrapped in the caller's account/region.
# Idempotent: safe to run repeatedly. Used as a one-time prerequisite before
# the first deploy in an account.
#
# NOTE: CDK bootstrap is an account-level prerequisite. If your organization
# manages account-level resources via a separate IaC pipeline, you may prefer to
# track the bootstrap footprint there. This script is the portable fallback /
# first-run path and what a CI runner uses when no separate IaC manages bootstrap.
#
# Usage:  AWS_REGION=us-east-1 ./scripts/relay-bootstrap.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/relay-context.sh"

relay_resolve_account

cd "${RELAY_ROOT}"
echo "Bootstrapping CDK in aws://${AWS_ACCOUNT_ID}/${AWS_REGION}" >&2
# Bootstrap executes the CDK app to discover environments but deploys nothing,
# so the container image isn't needed yet — skip the compute stack's fail-fast
# image guard (relay:image_check=false) so a first-run bootstrap works before
# any image is built. The deploy path still enforces the guard.
relay_cdk bootstrap "aws://${AWS_ACCOUNT_ID}/${AWS_REGION}" \
  -c relay:image_check=false
