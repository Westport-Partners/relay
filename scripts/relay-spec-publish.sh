#!/usr/bin/env bash
# relay-spec-publish.sh — attach a feature's Spec Kit artifacts to its GitHub issue.
#
# The specs/_active/<NNNN-name>/ working artifacts (spec, plan, research, data
# model, contracts, quickstart, tasks, checklists) are local-only — they never
# land in git history. When a feature ships we archive them onto the matching
# GitHub issue so the ticket carries the full design record. This script is the
# one place that does it, so the /dod walk, CI, and a human all post the same way.
#
# What it does:
#   1. Scans every file for secrets / 12-digit AWS account IDs / agency names
#      (hard rule: say "government agencies", never a specific agency). Aborts on
#      any hit — nothing is posted.
#   2. Assembles one Markdown comment, each artifact in a collapsible <details>.
#   3. Guards against double-posting (skips if a prior spec-archive comment with
#      the same marker exists, unless --force).
#   4. Posts to the issue with `gh issue comment` (or prints to stdout on --dry-run).
#
# Usage:
#   scripts/relay-spec-publish.sh <issue-number> <spec-dir>
#   scripts/relay-spec-publish.sh 40 specs/_active/0040-global-environment-filter
#   scripts/relay-spec-publish.sh 40 specs/_active/0040-... --dry-run   # print, don't post
#   scripts/relay-spec-publish.sh 40 specs/_active/0040-... --force     # repost even if present
#
# Exit code: 0 on success (posted, skipped-as-duplicate, or dry-run clean);
#            1 on a secret-scan hit or any error.
set -uo pipefail

MARKER="<!-- relay-spec-archive -->"
# GitHub hard-rejects comment bodies over 65536 chars.
MAX_CHARS=65000

ISSUE=""
SPEC_DIR=""
DRY_RUN=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force)   FORCE=1 ;;
    -*) echo "unknown arg: $1" >&2; exit 2 ;;
    *) if [ -z "$ISSUE" ]; then ISSUE="$1"; elif [ -z "$SPEC_DIR" ]; then SPEC_DIR="${1%/}"; else
         echo "unexpected arg: $1" >&2; exit 2; fi ;;
  esac
  shift
done

if [ -z "$ISSUE" ] || [ -z "$SPEC_DIR" ]; then
  echo "usage: $0 <issue-number> <spec-dir> [--dry-run] [--force]" >&2
  exit 2
fi
if ! [ -d "$SPEC_DIR" ]; then
  echo "spec dir not found: $SPEC_DIR" >&2
  exit 1
fi

# --- 1. Secret / account-ID / agency-name scan -----------------------------
# Account IDs: a bare 12-digit run. Test placeholders like 111111111111 /
# 123456789012 are allowed (all-same-digit or the well-known AWS docs value).
SCAN_FAIL=0
note_hit() { echo "  ✗ $1" >&2; SCAN_FAIL=1; }

while IFS= read -r f; do
  # 12-digit account IDs (skip obvious test placeholders)
  while IFS= read -r m; do
    [ -z "$m" ] && continue
    case "$m" in
      111111111111|000000000000|123456789012|999999999999) continue ;;
    esac
    note_hit "possible AWS account ID '$m' in $f"
  done < <(grep -oE '[0-9]{12}' "$f" | sort -u)

  # secrets / tokens
  if grep -inE 'glpat-[a-z0-9]|aws_secret|aws_access_key|-----BEGIN|bearer [a-z0-9]|password\s*[:=]\s*\S' "$f" >/dev/null 2>&1; then
    note_hit "possible secret/token in $f"
  fi

  # agency names (hard rule)
  if grep -inE 'uspto|patent and trademark|department of [a-z]' "$f" >/dev/null 2>&1; then
    note_hit "possible agency name in $f"
  fi
done < <(find "$SPEC_DIR" -type f -name '*.md' | sort)

if [ "$SCAN_FAIL" = 1 ]; then
  echo "Secret/account-ID/agency scan FAILED — nothing posted. Fix the spec artifacts first." >&2
  exit 1
fi

# --- 2. Idempotency guard --------------------------------------------------
if [ "$FORCE" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  if gh issue view "$ISSUE" --json comments \
       -q '.comments[].body' 2>/dev/null | grep -qF "$MARKER"; then
    echo "Issue #$ISSUE already has a spec-archive comment — skipping (use --force to repost)."
    exit 0
  fi
fi

# --- 3. Assemble the comment ----------------------------------------------
# Ordered so the narrative docs come first; only files that exist are included.
ORDER="spec.md plan.md research.md data-model.md \
       contracts/ui-contract.md contracts/ui-env-filter.md contracts/incidents-serialization.md \
       quickstart.md tasks.md checklists/requirements.md"

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT
{
  echo "$MARKER"
  echo "## 📄 Spec Kit artifacts — \`$SPEC_DIR\`"
  echo ""
  echo "Archived from the local \`specs/_active/\` working set now that the feature has shipped."
  echo "These artifacts are not in git history; this comment is the record. Secret / account-ID /"
  echo "agency-name scanned clean before posting."
  echo ""
  # Known order first, then any remaining .md files not already listed.
  printed=""
  emit() {
    local rel="$1" abs="$SPEC_DIR/$1"
    [ -f "$abs" ] || return 0
    case " $printed " in *" $rel "*) return 0 ;; esac
    printed="$printed $rel"
    echo "<details>"
    echo "<summary><b>$rel</b></summary>"
    echo ""
    echo '```markdown'
    cat "$abs"
    echo '```'
    echo ""
    echo "</details>"
    echo ""
  }
  for rel in $ORDER; do emit "$rel"; done
  while IFS= read -r abs; do
    emit "${abs#"$SPEC_DIR"/}"
  done < <(find "$SPEC_DIR" -type f -name '*.md' | sort)
} > "$BODY"

CHARS=$(wc -c < "$BODY")
echo "Assembled comment for issue #$ISSUE: ${CHARS} chars from $SPEC_DIR"

if [ "$CHARS" -gt "$MAX_CHARS" ]; then
  echo "✗ Comment is ${CHARS} chars, over GitHub's ${MAX_CHARS} limit." >&2
  echo "  Split the artifacts across multiple comments or attach a zip in the browser." >&2
  exit 1
fi

# --- 4. Post (or dry-run) --------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  echo "---- DRY RUN (not posting) ----"
  cat "$BODY"
  exit 0
fi

gh issue comment "$ISSUE" --body-file "$BODY"
