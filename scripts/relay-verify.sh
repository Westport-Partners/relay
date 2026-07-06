#!/usr/bin/env bash
# relay-verify.sh — run the automated "definition of done" gates for Relay.
#
# One place that the /dod slash command, CI, and a human all call, so the gates
# never drift between them (same convention as the deploy scripts: logic lives
# here, callers just invoke it).
#
# Blocking gates (a failure exits non-zero):
#   - ruff check        lint (matches CI: `ruff check .`)
#   - mypy              strict type check across the whole codebase (src, infra,
#                       tools, tests). Run per-root because src/relay and infra
#                       both contain top-level modules (e.g. app.py) that collide
#                       under a single mypy invocation. The backlog is at zero —
#                       keep it there.
#   - pytest -q         full offline test suite (matches CI)
#   - mkdocs --strict   docs site builds with no broken links/nav (only when
#                       docs/ or mkdocs.yml changed, or --docs/--all is passed)
#   - relay-synth.sh    CDK synth, no AWS writes (only when infra/ or the deploy
#                       scripts changed, or --infra/--all is passed)
#
# Usage:
#   scripts/relay-verify.sh            # auto-detect what to run from the git diff
#   scripts/relay-verify.sh --all      # force every gate (docs + infra too)
#   scripts/relay-verify.sh --docs     # force the docs gate
#   scripts/relay-verify.sh --infra    # force the infra/synth gate
#   scripts/relay-verify.sh --base origin/main   # diff base for auto-detect (default: main)
#
# Exit code: 0 if all BLOCKING gates pass, 1 otherwise. Advisory failures are
# printed but do not change the exit code.
set -uo pipefail

RELAY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RELAY_ROOT"

PY="${RELAY_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
run_tool() { "$PY" -m "$@"; }

BASE="main"
FORCE_DOCS=0
FORCE_INFRA=0
while [ $# -gt 0 ]; do
  case "$1" in
    --all)   FORCE_DOCS=1; FORCE_INFRA=1 ;;
    --docs)  FORCE_DOCS=1 ;;
    --infra) FORCE_INFRA=1 ;;
    --base)  shift; BASE="${1:-main}" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# Changed files vs. the base (best-effort; empty if not a git checkout / no base).
CHANGED="$(git diff --name-only "${BASE}"...HEAD 2>/dev/null; git diff --name-only 2>/dev/null; git diff --name-only --cached 2>/dev/null)"
changed_match() { printf '%s\n' "$CHANGED" | grep -qE "$1"; }

BLOCKING_FAIL=0
note_block() { echo "  ✗ BLOCKING FAILED: $1" >&2; BLOCKING_FAIL=1; }
note_pass()  { echo "  ✓ $1"; }

echo "== Relay verify (base=${BASE}) =="

# --- Lint (blocking) ---
echo "-- ruff check"
if run_tool ruff check src tests tools; then note_pass "ruff"; else note_block "ruff check"; fi

# --- Types (blocking) ---
# Per-root: src/relay and infra each have top-level modules (app.py, …) that
# collide if mypy is handed all roots at once. Any root failing blocks.
echo "-- mypy (src infra tools tests)"
mypy_ok=1
for _root in src infra tools tests; do
  [ -d "$_root" ] || continue
  if ! run_tool mypy "$_root"; then mypy_ok=0; fi
done
if [ "$mypy_ok" = 1 ]; then note_pass "mypy"; else note_block "mypy"; fi

# --- Tests (blocking) ---
echo "-- pytest -q"
if run_tool pytest -q; then note_pass "pytest"; else note_block "pytest"; fi

# --- Docs (blocking, conditional) ---
if [ "$FORCE_DOCS" = 1 ] || changed_match '^docs/|^mkdocs\.yml$'; then
  echo "-- mkdocs build --strict"
  if run_tool mkdocs build --strict -d /tmp/relay-verify-site >/dev/null 2>&1; then
    note_pass "mkdocs --strict"
  else
    echo "  (re-running to show the warnings)" >&2
    run_tool mkdocs build --strict -d /tmp/relay-verify-site 2>&1 | grep -iE 'WARNING|ERROR|Aborted' >&2 || true
    note_block "mkdocs build --strict"
  fi
else
  echo "-- mkdocs: skipped (no docs/ or mkdocs.yml change; use --docs to force)"
fi

# --- Infra synth (blocking, conditional) ---
if [ "$FORCE_INFRA" = 1 ] || changed_match '^infra/|^scripts/relay-(context|synth|deploy)'; then
  echo "-- relay-synth.sh (cdk synth, no AWS writes)"
  # This gate only validates that the CDK app synthesizes; it is not a real
  # deploy, so it must not require the developer to have deploy env vars set.
  # Supply placeholder identity inputs when unset (relay-context.sh needs a
  # team/org name to build context) — a real deploy always sets these itself.
  if [ -x "${RELAY_ROOT}/scripts/relay-synth.sh" ] \
    && RELAY_DEPLOY_TYPE="${RELAY_DEPLOY_TYPE:-team}" \
       RELAY_TEAM_NAME="${RELAY_TEAM_NAME:-verify}" \
       RELAY_ORG_ID="${RELAY_ORG_ID:-verify}" \
       "${RELAY_ROOT}/scripts/relay-synth.sh" >/dev/null 2>&1; then
    note_pass "cdk synth"
  else
    note_block "relay-synth.sh"
  fi
else
  echo "-- infra synth: skipped (no infra/ or deploy-script change; use --infra to force)"
fi

echo "== verify complete =="
if [ "$BLOCKING_FAIL" = 0 ]; then
  echo "All blocking gates passed."
  exit 0
fi
echo "One or more BLOCKING gates failed — not done yet." >&2
exit 1
