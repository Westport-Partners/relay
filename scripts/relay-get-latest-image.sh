#!/usr/bin/env bash
# relay-get-latest-image.sh — print the most recently pushed relay-hub ECR image URI.
#
# WHY THIS EXISTS
# ----------------
# scripts/relay-build-hub-image.sh prints a fully-qualified image URI that the
# operator is told to `export RELAY_HUB_IMAGE_URI=...` for a later deploy step.
# That export does not survive a shell-session boundary (a new terminal tab, a
# new SSH session, or separate automation tool calls) — the deploy step then
# fails with a misleading "relay:hub_image_uri is empty" error (ISSUE-12).
#
# This script re-derives the same URI on demand by asking ECR directly for the
# most recently pushed tag in the relay-hub repository, so a deploy step can
# resolve RELAY_HUB_IMAGE_URI independently of anything exported earlier:
#
#   RELAY_HUB_IMAGE_URI="$(./scripts/relay-get-latest-image.sh)"
#
# It does NOT build or push anything — it only reads what is already in ECR.
# Run relay-build-hub-image.sh first if the image you want isn't there yet.
#
# Usage:
#   ./scripts/relay-get-latest-image.sh
#
# Environment variables (all optional):
#   AWS_REGION / AWS_DEFAULT_REGION  — target region (default: us-east-1)
#
# Outputs (stdout, last line):
#   The fully-qualified ECR image URI of the most recently pushed
#   relay-hub image, e.g.:
#     123456789012.dkr.ecr.us-east-1.amazonaws.com/relay-hub:2309e2c
#
# Exit codes:
#   0  — URI printed
#   1  — no images found, repository doesn't exist, or an AWS call failed
set -euo pipefail

REPO_NAME="relay-hub"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

echo "=== Relay Hub latest image lookup ===" >&2
echo "Region: ${AWS_REGION}" >&2
echo "Repo:   ${REPO_NAME}" >&2

# ---------------------------------------------------------------------------
# 1. Resolve AWS account ID
# ---------------------------------------------------------------------------
if ! ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>&1)"; then
  echo "ERROR: could not resolve AWS account ID (aws sts get-caller-identity failed):" >&2
  echo "  ${ACCOUNT_ID}" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Find the most recently pushed image tag in the relay-hub ECR repository
# ---------------------------------------------------------------------------
_describe_output="$(aws ecr describe-images \
      --repository-name "${REPO_NAME}" \
      --region "${AWS_REGION}" \
      --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' \
      --output text 2>&1)" || {
  echo "ERROR: aws ecr describe-images failed for repository '${REPO_NAME}' in ${AWS_REGION}:" >&2
  echo "  ${_describe_output}" >&2
  echo "  If the repository does not exist yet, build and push an image first:" >&2
  echo "    ./scripts/relay-build-hub-image.sh" >&2
  exit 1
}

if [ -z "${_describe_output}" ] || [ "${_describe_output}" = "None" ]; then
  echo "ERROR: no images found in ECR repository '${REPO_NAME}' (${AWS_REGION})." >&2
  echo "  Build and push one first: ./scripts/relay-build-hub-image.sh" >&2
  exit 1
fi

IMAGE_TAG="${_describe_output}"
ECR_IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${REPO_NAME}:${IMAGE_TAG}"

echo "Latest tag: ${IMAGE_TAG}" >&2
echo "${ECR_IMAGE_URI}"
