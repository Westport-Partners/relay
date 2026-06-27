#!/usr/bin/env bash
# relay-entrypoint.sh — container entrypoint with optional self-running demo.
#
# Default behaviour is identical to running `relay-hub` directly: the Hub is
# exec'd as PID 1 so it receives SIGTERM for clean Fargate shutdown.
#
# When RELAY_DEMO=true, a background process waits for the Hub to come up and
# then runs the test-environment harness against http://localhost:<port>,
# populating the fleet + on-call + rules and trickling fake incidents. This lets
# someone see a realistic, populated Relay with nothing but:
#
#     docker run -e RELAY_DEMO=true -p 8080:8080 ghcr.io/westport-partners/relay:latest
#
# Demo knobs (all optional):
#   RELAY_DEMO=true                 enable the self-running harness
#   RELAY_DEMO_MODE=drip|once       drip (default) keeps the board live + evolving;
#                                   once seeds + a single incident burst, then stops
#   RELAY_DEMO_INTERVAL=20          seconds between drip incidents
#   RELAY_DEMO_SEED=42              world generation seed
#
# The harness is best-effort: if it fails, the Hub keeps running so the image is
# never bricked by demo tooling.
set -euo pipefail

PORT="${RELAY_PORT:-8080}"
DEMO="$(printf '%s' "${RELAY_DEMO:-false}" | tr '[:upper:]' '[:lower:]')"

if [ "$DEMO" = "true" ] || [ "$DEMO" = "1" ]; then
  echo "[entrypoint] RELAY_DEMO enabled — harness will populate this Hub on startup." >&2

  # Demo writes need an authenticated identity; force dev auth + open ingest
  # unless the operator already set them. (auth.py reads RELAY_AUTH_MODE, and
  # /ingest/* is gated on RELAY_ALLOW_INGEST or a local runtime.)
  export RELAY_AUTH_MODE="${RELAY_AUTH_MODE:-dev}"
  export RELAY_DEV_USER="${RELAY_DEV_USER:-operator}"
  export RELAY_ALLOW_INGEST="${RELAY_ALLOW_INGEST:-true}"

  (
    HARNESS_ARGS=(--base-url "http://localhost:${PORT}" --seed "${RELAY_DEMO_SEED:-42}")
    if [ "${RELAY_DEMO_MODE:-drip}" = "once" ]; then
      HARNESS_ARGS+=(--once)
    else
      HARNESS_ARGS+=(--interval "${RELAY_DEMO_INTERVAL:-20}")
    fi
    # harness.py waits for /health itself, so no sleep race here.
    if ! python /app/tools/testenv/harness.py "${HARNESS_ARGS[@]}"; then
      echo "[entrypoint] demo harness exited non-zero (Hub keeps running)." >&2
    fi
  ) &
fi

# Hand off to the Hub as the main process (PID 1 semantics for clean SIGTERM).
exec relay-hub
