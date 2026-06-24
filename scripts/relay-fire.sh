#!/usr/bin/env bash
# relay-fire.sh — fire a CloudWatch alarm fixture at a running Relay container.
#
# Reproduces the headline scenario in one command (collapsed-single-container
# plan §6): POST a real "CloudWatch Alarm State Change" event to /ingest/alarm,
# then read back the fleet tile + incidents so you can watch the tile go red.
#
# Usage:
#   ./scripts/relay-fire.sh [fixture.json] [base_url]
#     fixture.json  default: fixtures/alarms/lambda-error.json
#     base_url      default: http://localhost:8080
#
# Object-form alarm dimensions (the real AWS shape) are baked into the fixtures
# — the object-form shape is the real AWS shape and is what the pipeline parses.
set -euo pipefail

RELAY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE="${1:-${RELAY_ROOT}/fixtures/alarms/lambda-error.json}"
BASE_URL="${2:-http://localhost:8080}"

[ -f "$FIXTURE" ] || { echo "ERROR: fixture not found: $FIXTURE" >&2; exit 1; }

echo "Firing ${FIXTURE##*/} at ${BASE_URL}/ingest/alarm ..." >&2
curl -fsS -X POST "${BASE_URL}/ingest/alarm" \
  -H 'Content-Type: application/json' \
  --data @"${FIXTURE}"
echo >&2

echo "Fleet tiles:" >&2
curl -fsS "${BASE_URL}/fleet" || true
echo >&2
echo "Incidents:" >&2
curl -fsS "${BASE_URL}/incidents" || true
echo >&2
